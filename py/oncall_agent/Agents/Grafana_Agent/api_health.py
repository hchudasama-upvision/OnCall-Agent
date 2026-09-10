#!/usr/bin/env python3
"""
API response-code / success-rate alerts: the one dashboard, and the exact
number the on-call engineer looks at.

The owner pinned this on 2026-08-28: alerts like
    "US-Prod Response Codes - nginx -ai13s   aiWARE/prod"
are answered by "API Services - Overview"
(https://thanos-grafana.ops.veritone.com/d/da6b8dc3-ecd3-4b8c-b7e4e), which
carries every environment's response codes and success rate.

Two things make this alert family different from every other Grafana alert
here, and both are why this module exists rather than being left to the model:

1. THE ALERT NAME IS THE PANEL TITLE. "US-Prod Response Codes - nginx -ai13s"
   is panel 25's title verbatim, and its paired success-rate stat is panel 26.
   So panel selection is a lookup against the dashboard's own titles, read
   live — not a static map that goes stale, and not a judgment call.

2. THE NUMBER DECIDES A SECOND POST. The runbook (DESIGN.md §2, "wait 5-10 min
   for self-heal") says to re-check and report recovery, and the owner's
   threshold is 99.99%. A threshold comparison that decides whether to post is
   a CODE decision (CLAUDE.md non-negotiable #1), so the rate is computed here,
   deterministically, from the panel's own queries — never read off a picture
   or estimated by the model. follow_up.py owns the timing.

How the rate is computed
------------------------
These panels are ELASTICSEARCH-backed, not Prometheus (datasource
`es-nginx-prod` -> elasticsearch-elk-lb.aws-prod.veritone.com). Panel 26's own
math expression, read from the dashboard on 2026-08-28, is:

    1 - (5XX / (2XX + 5XX + 4XX + NEG))

where each term is a Lucene count over the window and the panel's unit is
`percentunit` with 4 decimals — so 0.9999 displays as "99.99%". This module
replays the panel's OWN four count queries through Grafana's /api/ds/query and
applies that same formula, so the number in the thread is the number on the
dashboard. Verified against panel 25's own legend on 2026-08-28: 2XX 1.08 Mil,
4XX 6.59K, 5XX 30, NEG 0 -> 99.9972%.

Gotchas found while building this, kept so nobody rediscovers them:
  * Grafana's Elasticsearch proxy REFUSES POST except on /_msearch, so the
    datasource-proxy route is a dead end for aggregations; /api/ds/query is
    the supported path.
  * The ES datasource rejects a query with no aggregation ("invalid query,
    missing metrics and aggregations"), so the panel's own date_histogram is
    kept and the buckets are summed — which is exactly what the panel's
    `reduce` stage does before its math.
  * A returned frame is [time_column, count_column]. Summing every numeric
    value adds epoch milliseconds to the count; the first attempt here read
    6.4e15 requests. Only the non-time field is summed.
"""
import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import grafana

DASHBOARD_UID = os.environ.get("API_HEALTH_DASHBOARD_UID", "da6b8dc3-ecd3-4b8c-b7e4e")

# The owner's threshold (2026-08-28): at or above this, the API is considered
# recovered and that gets said in the thread. A ratio, matching the panel's
# `percentunit`, so 0.9999 == 99.99%.
RECOVERY_THRESHOLD = float(os.environ.get("API_RECOVERY_THRESHOLD", "0.9999"))

# What "right now" means when re-checking. Short enough that a recovery shows
# up, long enough that a quiet minute does not read as an outage.
DEFAULT_WINDOW = os.environ.get("API_HEALTH_WINDOW", "now-15m")

_ALERT_HINT = re.compile(r"(response\s*codes?|success\s*rate)", re.I)


@dataclass
class PanelPair:
    """The two panels that answer one environment's API-health alert."""
    environment: str            # "US-Prod ... nginx -ai13s", as the dashboard titles it
    codes_panel: int            # the response-codes timeseries (what to screenshot)
    rate_panel: int             # the success-rate stat (where the number comes from)
    codes_title: str = ""
    rate_title: str = ""


@dataclass
class SuccessRate:
    ratio: float                        # 0.999972
    counts: Dict[str, int] = field(default_factory=dict)
    window: str = DEFAULT_WINDOW
    panel: int = 0

    @property
    def percent(self) -> str:
        """Four decimals, the way the panel formats it."""
        return f"{self.ratio * 100:.4f}%"

    @property
    def recovered(self) -> bool:
        return self.ratio >= RECOVERY_THRESHOLD

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def is_api_health_alert(alert_name: str, raw_text: str = "") -> bool:
    """Does this alert belong to this dashboard?

    Deliberately narrow: the phrase "Response Codes" or "API Success Rate".
    Anything vaguer would pull in unrelated nginx/API alerts that have their
    own evidence (the ApiSuccessRate-nginx case, for one, is a log
    investigation rather than this dashboard).
    """
    return bool(_ALERT_HINT.search(alert_name or "") or _ALERT_HINT.search(raw_text or ""))


def _dashboard() -> dict:
    raw, _ = grafana._request(f"/api/dashboards/uid/{DASHBOARD_UID}")
    return json.loads(raw)


def _panels(dash: dict) -> List[dict]:
    def walk(panels):
        for panel in panels or []:
            if panel.get("type") == "row":
                yield from walk(panel.get("panels"))
                continue
            yield panel
    return list(walk(dash.get("dashboard", {}).get("panels")))


def _normalize(text: str) -> str:
    """Compare titles the way a human reads them, not byte for byte.

    A VictorOps line arrives as "US-Prod Response Codes - nginx -ai13s
    aiWARE/prod" — extra spacing, an environment suffix, sometimes different
    dashes. Panel titles are stable; the alert text around them is not.
    """
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


# Words that appear in every title on this dashboard and so distinguish nothing.
# "nginx"/"haproxy" are dropped here because only ONE side of a pair carries
# them (the codes panel); `-ai13s` is handled as a gate instead, since it is the
# one token that genuinely separates two ingresses in the same region.
_GENERIC_TITLE_WORDS = {
    "api", "apis", "success", "rate", "response", "responses", "code", "codes",
    "nginx", "haproxy", "ai13s", "overview", "the", "and",
}


def _region_tokens(normalized_title: str) -> set:
    return {token for token in normalized_title.split()
            if token not in _GENERIC_TITLE_WORDS}


def find_panel_pair(alert_name: str) -> Optional[PanelPair]:
    """The response-codes panel this alert names, and its success-rate twin.

    Matched against the dashboard's live titles rather than a static table:
    the alert IS the panel title, so a new environment added to the dashboard
    works with no code change here. Returns None when nothing matches, which
    the caller must treat as "no pinned panel", not as a reason to guess.
    """
    panels = _panels(_dashboard())
    titles = {p.get("id"): (p.get("title") or "") for p in panels}
    alert = _normalize(alert_name)
    if not alert:
        return None

    # Longest title first: "US-Prod Response Codes - nginx -ai13s" must win over
    # a shorter "US-Prod Response Codes - haproxy" that also prefixes it.
    codes_id, codes_title = None, ""
    for pid, title in sorted(titles.items(), key=lambda kv: -len(kv[1])):
        norm = _normalize(title)
        if not norm or "response code" not in norm:
            continue
        if norm in alert or alert.startswith(norm):
            codes_id, codes_title = pid, title
            break
    if codes_id is None:
        return None

    # The success-rate stat for the same environment. The dashboard names them
    # as pairs — "US-Prod Response Codes - nginx -ai13s" / "US-Prod API Success
    # Rate -ai13s" — but the pairing is not naive prefix matching, and getting
    # it wrong is worse than finding nothing: it puts one environment's number
    # under another environment's graph. Two rules, both derived from the real
    # titles on 2026-08-28:
    #   * `-ai13s` is a HARD GATE, not a token. "US-Prod Response Codes -
    #     haproxy" pairs with "Prod - API Success Rate" (panel 12); the ai13s
    #     stat (panel 26) is a different ingress entirely.
    #   * an EXTRA region token in the candidate is a penalty, not neutral.
    #     Without that, "US-Prod ... haproxy" {us, prod} scored the same on
    #     "UK-Prod API Success Rate" {uk, prod} as on "Prod - API Success Rate"
    #     {prod}, and picked whichever came first.
    codes_norm = _normalize(codes_title)
    wants_ai13s = "ai13s" in codes_norm
    codes_tokens = _region_tokens(codes_norm)

    best, best_score, best_title = 0, 0, ""
    for pid, title in titles.items():
        norm = _normalize(title)
        if "success rate" not in norm:
            continue
        if ("ai13s" in norm) != wants_ai13s:
            continue
        candidate = _region_tokens(norm)
        score = len(codes_tokens & candidate) - len(candidate - codes_tokens)
        if score > best_score:
            best, best_score, best_title = pid, score, title
    # rate_panel 0 means "no confident twin" — the caller reports no number
    # rather than quoting a neighbouring environment's.
    return PanelPair(environment=codes_title, codes_panel=codes_id, rate_panel=best,
                     codes_title=codes_title, rate_title=best_title)


def _substitute_constants(query: str, dash: dict) -> str:
    """Fill $constant dashboard variables the way Grafana would.

    Only `constant` and `textbox` variables — a query variable would need its
    own datasource round trip, and this dashboard's one variable
    ($upstreamNamesAll) is a constant listing the four core upstreams.
    """
    for var in dash.get("dashboard", {}).get("templating", {}).get("list") or []:
        if var.get("type") not in ("constant", "textbox"):
            continue
        value = var.get("query") or (var.get("current") or {}).get("value") or ""
        if isinstance(value, list):
            value = ",".join(str(v) for v in value)
        for form in (f"${var.get('name')}", "${" + str(var.get("name")) + "}"):
            query = query.replace(form, str(value))
    return query


def _frame_total(result: dict) -> int:
    """Sum one query's count buckets.

    A frame is [time_column, count_column]; only the non-time field counts.
    Summing both silently adds epoch milliseconds to the request count.
    """
    total = 0
    for frame in result.get("frames") or []:
        fields = frame.get("schema", {}).get("fields") or []
        values = frame.get("data", {}).get("values") or []
        for index, field_spec in enumerate(fields):
            if field_spec.get("type") == "time" or field_spec.get("name") == "@timestamp":
                continue
            if index < len(values):
                total += sum(v for v in values[index] if isinstance(v, (int, float)))
    return int(total)


def success_rate(rate_panel: int, from_: str = DEFAULT_WINDOW, to: str = "now") -> SuccessRate:
    """The success rate the panel itself would show, computed from its own queries.

    Raises RuntimeError if the panel's queries cannot be run or the window has
    no requests at all — an empty window is not a 100% success rate, and
    reporting it as one would be the exact kind of confident nonsense the
    no-fabrication rule exists to stop.
    """
    dash = _dashboard()
    panel = next((p for p in _panels(dash) if p.get("id") == rate_panel), None)
    if not panel:
        raise RuntimeError(f"panel {rate_panel} not found on dashboard {DASHBOARD_UID}")

    queries, aliases = [], {}
    for target in panel.get("targets") or []:
        query = target.get("query")
        if not query:                       # the reduce/math stages have no query
            continue
        ref = target.get("refId") or str(len(queries))
        aliases[ref] = (target.get("alias") or ref).lower()
        queries.append({
            "refId": ref,
            "datasource": target.get("datasource") or panel.get("datasource"),
            "query": _substitute_constants(query, dash),
            "timeField": target.get("timeField") or "@timestamp",
            "metrics": target.get("metrics") or [
                {"id": "1", "type": "count", "field": "select field"}],
            # Keep the panel's own bucketing: the datasource rejects a query
            # with no aggregation, and summing its buckets is what the panel's
            # reduce stage does.
            "bucketAggs": target.get("bucketAggs") or [
                {"id": "2", "type": "date_histogram", "field": "@timestamp",
                 "settings": {"interval": "auto", "min_doc_count": 0, "trimEdges": 0}}],
        })
    if not queries:
        raise RuntimeError(f"panel {rate_panel} has no runnable queries")

    raw, _ = grafana._request("/api/ds/query", method="POST",
                              body={"from": from_, "to": to, "queries": queries})
    results = json.loads(raw).get("results") or {}
    counts: Dict[str, int] = {}
    for ref, result in results.items():
        if result.get("error"):
            raise RuntimeError(f"panel {rate_panel} query {ref} failed: "
                               f"{str(result['error'])[:200]}")
        counts[aliases.get(ref, ref)] = _frame_total(result)

    total = sum(counts.values())
    if not total:
        raise RuntimeError(
            f"no requests at all in {from_} -> {to} for panel {rate_panel} — that is not a "
            f"100% success rate, it is no data; report it as no data")
    server_errors = sum(v for k, v in counts.items() if k.startswith("5"))
    return SuccessRate(ratio=1 - (server_errors / total), counts=counts,
                       window=f"{from_} -> {to}", panel=rate_panel)


def dashboard_url(from_: str = "now-1h", to: str = "now") -> str:
    """The dashboard link a human would open, built from the configured base."""
    base = (os.environ.get("GRAFANA_URL") or "").rstrip("/")
    return (f"{base}/d/{DASHBOARD_UID}/api-services-overview"
            f"?from={from_}&to={to}&orgId=1&timezone=browser")


def _selftest(alert_name: str) -> int:
    """python -m oncall_agent.Agents.Grafana_Agent.api_health "<alert name>" """
    import sys
    pair = find_panel_pair(alert_name)
    if not pair:
        print(f"no panel matches {alert_name!r}", file=sys.stderr)
        return 1
    print(f"alert:        {alert_name}\n"
          f"codes panel:  {pair.codes_panel} — {pair.codes_title}\n"
          f"rate panel:   {pair.rate_panel} — {pair.rate_title}", file=sys.stderr)
    rate = success_rate(pair.rate_panel)
    print(f"window:       {rate.window}\n"
          f"counts:       {rate.counts}\n"
          f"success rate: {rate.percent}  (threshold "
          f"{RECOVERY_THRESHOLD * 100:.4f}% -> "
          f"{'RECOVERED' if rate.recovered else 'still below'})", file=sys.stderr)
    print(f"url:          {dashboard_url()}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    import sys
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    sys.exit(_selftest(" ".join(sys.argv[1:]) or "US-Prod Response Codes - nginx -ai13s"))
