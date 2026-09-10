#!/usr/bin/env python3
"""
Grafana tools for the investigating model — search, inspect, query, render.

Why this exists
---------------
The first version of this repo encoded every alert->dashboard decision into
config/panel_map.json: dashboard uid, panel ids, which Alertmanager label
index fills which template variable, and a PromQL lookup to translate a
hostname into an instance. That is a lot of brittle static configuration, and
it has to be re-derived by hand for every new alert type.

The owner's direction (2026-08-24) is the opposite: cases.json says WHAT to
do for an alert type, and Claude works out the specifics live against the
Grafana API. So these tools exist to let it do that — find the dashboard,
read what a panel actually queries, check the metric exists for this host,
render it, and see whether the render came back empty.

The feedback loop is the point. A statically-configured variable that matches
nothing renders a green "N/A" gauge and nobody finds out until it is in the
incident thread. render_panel returns `status: "empty"` instead, so the model
can look at why and try the right variable — the same loop a human runs.

Read-only plus rendering. Nothing here can modify a dashboard, and nothing
here can post to Slack.
"""
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

from ... import chart
from . import api_health, grafana, grafana_capture, thanos

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "noc-grafana", "version": "0.1.0"}

# Rendered panels land here and are recorded in a manifest the caller reads
# back, so an investigation can create evidence that did not exist when the
# run started. Keys are what the model cites in evidence_keys.
EVIDENCE_DIR = Path(os.environ.get("EVIDENCE_DIR")
                    or Path(__file__).resolve().parents[4] / "dist" / "evidence")
MANIFEST = "rendered-panels.json"

_DEFAULT_DS = os.environ.get("PROM_DATASOURCE_UID", "thanos-main-ds")


def _record(key: str, path: Path, caption: str) -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = EVIDENCE_DIR / MANIFEST
    data = {}
    if manifest_path.exists():
        try:
            data = json.loads(manifest_path.read_text())
        except ValueError:
            data = {}
    data[key] = {"path": str(path), "caption": caption}
    manifest_path.write_text(json.dumps(data, indent=2))


def load_manifest(evidence_dir: Optional[Path] = None) -> Dict[str, dict]:
    path = (evidence_dir or EVIDENCE_DIR) / MANIFEST
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except ValueError:
        return {}


def clear_manifest(evidence_dir: Optional[Path] = None) -> None:
    """Start each investigation with an empty manifest.

    Otherwise a panel rendered for a previous alert stays citable, and the
    model could attach last incident's graph to this one.
    """
    path = (evidence_dir or EVIDENCE_DIR) / MANIFEST
    if path.exists():
        path.unlink()


# --------------------------------------------------------------------- tools

def tool_search_dashboards(query: str = "", limit: int = 20) -> str:
    hits = grafana.search_dashboards(query, limit=limit)
    if not hits:
        return f"(no dashboards match {query!r})"
    return "\n".join(f"{d.get('uid')}  {d.get('title')}" for d in hits)


def tool_describe_dashboard(dashboard_uid: str) -> str:
    """Template variables AND each panel's query — the two things that decide
    whether a render will work.

    The queries matter: a panel titled "Memory Utilization" may filter on
    $instance while the dashboard also offers $hostname, so setting the
    obvious-looking variable produces an empty panel. Showing the expression
    is what lets that be worked out rather than discovered in Slack.
    """
    raw, _ = grafana._request(f"/api/dashboards/uid/{dashboard_uid}")
    dash = json.loads(raw).get("dashboard", {})
    lines = [f"dashboard: {dash.get('title')} [{dashboard_uid}]", "", "TEMPLATE VARIABLES:"]
    for var in dash.get("templating", {}).get("list") or []:
        current = var.get("current", {}).get("value")
        if isinstance(current, list):
            current = current[:3]
        lines.append(f"  ${var.get('name')}  type={var.get('type')}")
        lines.append(f"      query:   {str(var.get('query'))[:160]}")
        lines.append(f"      example: {current}")

    lines += ["", "PANELS (id, type, title, query):"]

    def walk(panels):
        for panel in panels or []:
            if panel.get("type") == "row":
                yield from walk(panel.get("panels"))
                continue
            yield panel

    for panel in walk(dash.get("panels")):
        exprs = [(t.get("expr") or t.get("rawSql") or "") for t in (panel.get("targets") or [])]
        expr = next((e for e in exprs if e), "")
        lines.append(f"  {panel.get('id')}  {panel.get('type'):<12} {panel.get('title')}")
        if expr:
            lines.append(f"      {expr[:200]}")
    return "\n".join(lines)


def tool_prometheus_query(expr: str, datasource_uid: str = "") -> str:
    """Instant PromQL query. Use it to confirm the metric exists for this host
    BEFORE rendering, and to translate one label into another (hostname ->
    instance, pod -> volume)."""
    ds = datasource_uid or _DEFAULT_DS
    url = (f"/api/datasources/proxy/uid/{ds}/api/v1/query"
           f"?query={urllib.parse.quote(expr)}")
    raw, _ = grafana._request(url)
    payload = json.loads(raw)
    if payload.get("status") != "success":
        return f"query failed: {json.dumps(payload)[:300]}"
    results = payload.get("data", {}).get("result") or []
    if not results:
        return f"(0 series for {expr!r} — the metric does not exist for those labels)"
    lines = [f"{len(results)} series:"]
    for series in results[:20]:
        metric = series.get("metric") or {}
        value = (series.get("value") or ["", ""])[1]
        labels = " ".join(f"{k}={v}" for k, v in sorted(metric.items()) if k != "__name__")
        lines.append(f"  value={value}  {labels[:220]}")
    if len(results) > 20:
        lines.append(f"  … {len(results) - 20} more")
    return "\n".join(lines)


def tool_list_datasources() -> str:
    raw, _ = grafana._request("/api/datasources")
    rows = [d for d in json.loads(raw) if d.get("type") in ("prometheus", "loki")]
    return "\n".join(f"{d['uid']:<24} {d['type']:<12} {d['name']}" for d in rows) or "(none)"


def tool_render_panel(dashboard_uid: str, panel_id: int, variables: Optional[dict] = None,
                      from_: str = "now-6h", to: str = "now", key: str = "") -> str:
    """Render one panel to a PNG that can be attached to the Slack thread.

    Returns the evidence key to cite in evidence_keys. An empty render is
    reported as such and NOT saved — an empty gauge still draws its coloured
    arc, so posting one would look like a real measurement of nothing.
    """
    variables = {k: str(v) for k, v in (variables or {}).items()}
    key = key or f"grafana_{dashboard_uid[:8]}_{panel_id}".replace("-", "_")
    out_path = EVIDENCE_DIR / f"{key}.png"
    caption = (f"Grafana {dashboard_uid} panel {panel_id} · {from_} → {to}"
               + (" · " + " ".join(f"{k}={v}" for k, v in variables.items()) if variables else ""))
    try:
        with grafana_capture.grafana_browser() as context:
            grafana_capture.capture_panel(context, dashboard_uid, panel_id, out_path,
                                          from_=from_, to=to, variables=variables)
    except grafana_capture.NoDataError as e:
        return (f"EMPTY — {e}\n"
                f"The panel rendered but has no series. Check with prometheus_query "
                f"whether the metric exists for these labels, and read the panel's own "
                f"query in describe_dashboard: it may filter on a different variable "
                f"than the one you set. Nothing was saved.")
    except Exception as e:                          # noqa: BLE001
        return f"RENDER FAILED — {type(e).__name__}: {str(e).splitlines()[0]}"

    _record(key, out_path, caption)
    return (f"OK — rendered {out_path.stat().st_size} bytes.\n"
            f"evidence_key: {key}\ncaption: {caption}\n"
            f"Cite \"{key}\" in a post's evidence_keys to attach it.")


def tool_api_success_rate(alert_name: str, window: str = "") -> str:
    """The API success rate for a response-code alert, computed from the
    dashboard panel's OWN queries.

    This exists because "API Services - Overview" is Elasticsearch-backed:
    prometheus_query cannot answer it, describe_dashboard shows the query but
    not the value, and a rendered stat panel is a picture a model should not be
    reading numbers off. Without this tool the first post carried a graph and
    the words "its current value is not visible to me" (real output,
    2026-08-28).
    """
    try:
        pair = api_health.find_panel_pair(alert_name)
    except Exception as e:                              # noqa: BLE001
        return f"LOOKUP FAILED — {type(e).__name__}: {str(e).splitlines()[0]}"
    if not pair:
        return (f"No panel on 'API Services - Overview' matches {alert_name!r}. Do not guess "
                f"a rate; say the dashboard does not cover this alert.")
    lines = [f"dashboard: API Services - Overview [{api_health.DASHBOARD_UID}]",
             f"response-codes panel: {pair.codes_panel} — {pair.codes_title}",
             f"success-rate panel:   {pair.rate_panel} — {pair.rate_title}",
             "RENDER BOTH of those panels and attach both — the stat is the number a human "
             "looks for first, the timeseries says which codes moved."]
    if not pair.rate_panel:
        lines.append("No confident success-rate twin for this environment — report the graph "
                     "only, and do not quote a neighbouring environment's number.")
        return "\n".join(lines)
    try:
        rate = api_health.success_rate(pair.rate_panel, from_=window or api_health.DEFAULT_WINDOW)
    except Exception as e:                              # noqa: BLE001
        lines.append(f"RATE UNAVAILABLE — {type(e).__name__}: {str(e).splitlines()[0]}")
        return "\n".join(lines)
    lines += [
        f"window: {rate.window}",
        f"success rate: {rate.percent}  (panel formula 1 - 5XX/(2XX+5XX+4XX+NEG), "
        f"4 decimals as the panel shows it)",
        "counts: " + ", ".join(f"{band}={count}" for band, count in sorted(rate.counts.items())),
        f"threshold {api_health.RECOVERY_THRESHOLD * 100:.4f}% -> "
        f"{'at or above' if rate.recovered else 'BELOW'}",
        "A re-check is armed automatically 5 and 10 minutes after your post, and the "
        "recovered/still-below line plus a fresh screenshot is posted by code. Do not "
        "promise a re-check, and do not ask a human to watch it.",
    ]
    return "\n".join(lines)


# A monitored endpoint URL can carry a live credential in its query string —
# the real ops-prom EndpointDown series on 2026-08-31 included
# "...?authToken=<real token>". These threads get posted to Slack, so the query
# string is stripped in CODE rather than trusted to the prompt.
_URL_TOKENISH = re.compile(r"(?i)\b(auth|api|access|session|token|key|secret|password|sig)")


def _safe_url(url: str) -> str:
    """Host + path, with a query string reduced to its parameter NAMES.

    Keeping the names matters — "?authToken=..." vs "?playout" is the
    difference between two endpoints — while the values never reach Slack.
    """
    if "?" not in url:
        return url
    base, _, query = url.partition("?")
    if not query:
        return f"{base}?"
    names = []
    for part in query.split("&"):
        name = part.split("=", 1)[0]
        names.append(f"{name}=<redacted>" if _URL_TOKENISH.search(name) else name)
    return f"{base}?{'&'.join(names)}"


def tool_endpoint_status(url_filter: str = "", state: str = "firing", limit: int = 20) -> str:
    """Which monitored endpoints are down right now, from the alert series itself.

    EndpointDown (env=ops-prom, monitor=OpsProm) carries everything the thread
    needs on the series' own labels: the `url` probed, the `status` it returned
    versus `expected_status_code`, `total_time`, and the TLS fields
    (cert_available / cert_expiry_days / expiry_date / cert_issuer). Reading
    them here means the thread names the endpoint and the failure mode instead
    of restating the alert.

    Credentials in a URL's query string are stripped before returning (see
    _safe_url) — quote the URL exactly as this tool gives it to you.
    """
    selector = 'ALERTS{alertname="EndpointDown"'
    if state in ("firing", "pending"):
        selector += f', alertstate="{state}"'
    selector += "}"
    try:
        series = grafana.instant_query(_DEFAULT_DS, selector)
    except Exception as e:                              # noqa: BLE001
        return f"QUERY FAILED — {type(e).__name__}: {str(e).splitlines()[0]}"
    if not series:
        return (f"No EndpointDown series in state {state!r} right now. The probe may have "
                f"recovered since the alert fired — say that, and check `total_time` history "
                f"rather than assuming it never happened.")

    rows = []
    for item in series:
        labels = item.get("metric", {})
        url = labels.get("url", "")
        if url_filter and url_filter.lower() not in url.lower():
            continue
        cert = labels.get("cert_expiry_days", "")
        rows.append({
            "url": _safe_url(url),
            "status": labels.get("status", "?"),
            "expected": labels.get("expected_status_code", "?"),
            "total_time": labels.get("total_time", "?"),
            "state": labels.get("alertstate", state),
            "environment": labels.get("environment", ""),
            "cert": (f"{cert}d left, issuer {labels.get('cert_issuer', '?')}, expires "
                     f"{labels.get('expiry_date', '?')}"
                     if cert not in ("", "N/A") else
                     f"cert_available={labels.get('cert_available', '?')}"),
        })
    if not rows:
        return (f"No EndpointDown series matches {url_filter!r} (state {state!r}). "
                f"{len(series)} endpoint(s) are down overall — do not report one you did "
                f"not match.")

    lines = [f"{len(rows)} endpoint(s) in state {state!r}"
             + (f" matching {url_filter!r}" if url_filter else "") + ":"]
    for row in rows[:max(1, min(int(limit), 100))]:
        lines.append(f"  {row['url']}")
        lines.append(f"      got {row['status']}, expected {row['expected']}, "
                     f"probe {row['total_time']}s, state {row['state']}")
        lines.append(f"      tls: {row['cert']}"
                     + (f"   monitor group: {row['environment']}" if row["environment"] else ""))
    if len(rows) > limit:
        lines.append(f"  ... {len(rows) - limit} more not shown; the count above is the total.")
    lines.append("URLs above already have credential-bearing query values redacted — quote "
                 "them exactly as shown, never reconstruct the original.")
    return "\n".join(lines)


def tool_thanos_alert_status(alert_name: str, instance: str = "", hours: int = 6) -> str:
    """For a PromQL-rule alert ("High concurrent_requests for core-admin-server"):
    the rule's own threshold, the current value, and HOW LONG it has been on that
    side of the threshold.

    The alert title is the rule name, so the threshold and the exact selector are
    looked up in Thanos rather than guessed — and the answer says "high for 2h10m"
    or "low again, came back below 12m ago" instead of a bare number.
    """
    try:
        rules = thanos.find_alert_rules(alert_name)
    except thanos.ThanosError as e:
        return f"THANOS LOOKUP FAILED — {e}"
    if not rules:
        return (f"No alerting rule in Thanos matches {alert_name!r}. Without the rule there is "
                f"no threshold, so do not call a number high or low — say the rule could not "
                f"be found.")
    lines = []
    for rule in rules[:3]:
        lines.append(f"rule:      {rule.name}   [{rule.group}]")
        lines.append(f"condition: {rule.query}   (for {rule.for_seconds // 60}m, "
                     f"currently {rule.state})")
        try:
            series, note = thanos.resolve_series(rule.expr, instance=instance)
        except thanos.ThanosError as e:
            lines.append(f"  series unavailable: {e}")
            continue
        lines.append(f"  scope:   {note}")
        if series:
            values = sorted(((float(s["value"][1]), s["metric"].get("instance", "?"),
                              s["metric"].get("pod", ""))
                             for s in series), reverse=True)
            lines.append(f"  pods:    {len(values)} reporting; worst first:")
            for value, host, pod in values[:6]:
                lines.append(f"      {value:>8.0f}  {host}" + (f"  {pod}" if pod else ""))
        try:
            summary = thanos.breach_summary(rule.expr, rule.threshold, hours=hours)
            lines.append(f"  now:     {summary.explain()}")
            lines.append(f"  window:  last {hours}h, {summary.points} datapoint(s), "
                         f"min {summary.minimum:.0f} / max {summary.maximum:.0f}")
        except thanos.ThanosError as e:
            lines.append(f"  history unavailable: {e}")
        lines.append(f"  graph:   {thanos.graph_url(rule.expr, hours)}")
    lines.append("The threshold and the `for` duration come from the rule itself. Quote the "
                 "duration wording as given — it is computed from the series, not estimated.")
    return "\n".join(lines)


def tool_thanos_graph(expr: str, hours: int = 6, key: str = "", threshold: float = 0.0) -> str:
    """Screenshot the Thanos graph for a PromQL expression and return its evidence key.

    Captures the real Thanos UI (query bar in frame, so the picture carries its
    own provenance). Falls back to drawing the series locally if the UI will not
    render, and says which one produced the image.
    """
    key = key or "thanos_" + re.sub(r"[^a-z0-9]+", "_", expr.lower())[:48].strip("_")
    out_path = EVIDENCE_DIR / f"{key}.png"
    caption = f"Thanos · {expr} · last {hours}h"
    try:
        thanos.capture_graph(expr, out_path, hours=hours)
        _record(key, out_path, caption)
        return (f"OK — captured {out_path.stat().st_size} bytes from the Thanos UI.\n"
                f"evidence_key: {key}\ncaption: {caption}\n"
                f"Cite \"{key}\" in a post's evidence_keys to attach it.")
    except Exception as ui_error:                       # noqa: BLE001
        pass
    try:
        points = thanos.query_range(f"max({expr})", hours=hours)
        if not points:
            return (f"NO GRAPH — the Thanos UI would not render and the expression returns no "
                    f"datapoints over {hours}h. Report no data; do not describe a graph.")
        from datetime import datetime, timezone
        series = [(datetime.fromtimestamp(ts, timezone.utc), value) for ts, value in points]
        chart.render_series_png(series, f"{expr}", out_path,
                                subtitle=f"Thanos · last {hours}h · drawn locally",
                                threshold=threshold or None)
    except Exception as e:                              # noqa: BLE001
        return f"CAPTURE FAILED — Thanos UI: {str(ui_error)[:120]}; local fallback: {e}"
    local_caption = f"{caption} — drawn locally from Thanos data (the Thanos UI did not render)"
    _record(key, out_path, local_caption)
    return (f"OK — the Thanos UI did not render, so the series was drawn locally "
            f"({out_path.stat().st_size} bytes).\nevidence_key: {key}\n"
            f"caption: {local_caption}\nCite \"{key}\" in a post's evidence_keys.")


TOOLS = [
    {"name": "search_dashboards",
     "description": "Find Grafana dashboards by title. Start here: search for the resource "
                    "the alert is about (memory, volume, disk, vmware, node).",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "limit": {"type": "integer"}}},
     "handler": tool_search_dashboards},
    {"name": "describe_dashboard",
     "description": "A dashboard's template variables AND every panel's id, title and "
                    "underlying query. Read this before rendering — the query tells you "
                    "which variable the panel actually filters on.",
     "inputSchema": {"type": "object", "properties": {
         "dashboard_uid": {"type": "string"}}, "required": ["dashboard_uid"]},
     "handler": tool_describe_dashboard},
    {"name": "prometheus_query",
     "description": "Run an instant PromQL query. Use it to confirm a metric exists for "
                    "this host before rendering, or to translate one label into another "
                    "(e.g. windows_os_hostname{hostname=\"SVC182\"} to get its instance).",
     "inputSchema": {"type": "object", "properties": {
         "expr": {"type": "string"}, "datasource_uid": {"type": "string"}},
         "required": ["expr"]},
     "handler": tool_prometheus_query},
    {"name": "list_datasources",
     "description": "Prometheus/Loki datasource uids available for prometheus_query.",
     "inputSchema": {"type": "object", "properties": {}},
     "handler": tool_list_datasources},
    {"name": "thanos_alert_status",
     "description": "For a PromQL-rule alert (e.g. 'High concurrent_requests for "
                    "core-admin-server'): the rule's own threshold and selector from Thanos, "
                    "the current per-pod values, and HOW LONG it has been above or below the "
                    "threshold — 'high for 2h10m' / 'low again, came back below 12m ago'. "
                    "START HERE for those alerts; the alert title is the rule name.",
     "inputSchema": {"type": "object", "properties": {
         "alert_name": {"type": "string"}, "instance": {"type": "string"},
         "hours": {"type": "integer"}}, "required": ["alert_name"]},
     "handler": tool_thanos_alert_status},
    {"name": "thanos_graph",
     "description": "Screenshot the Thanos UI graph for a PromQL expression (query bar in "
                    "frame) and return its evidence key. Use the rule's own expression from "
                    "thanos_alert_status.",
     "inputSchema": {"type": "object", "properties": {
         "expr": {"type": "string"}, "hours": {"type": "integer"},
         "key": {"type": "string"}, "threshold": {"type": "number"}},
         "required": ["expr"]},
     "handler": tool_thanos_graph},
    {"name": "endpoint_status",
     "description": "For an EndpointDown alert (env=ops-prom / monitor=OpsProm): which "
                    "endpoints are down right now, what status each returned versus the "
                    "expected one, probe time, and TLS expiry — read from the alert series' "
                    "own labels. Credentials in a URL query string are redacted for you.",
     "inputSchema": {"type": "object", "properties": {
         "url_filter": {"type": "string"}, "state": {"type": "string"},
         "limit": {"type": "integer"}}},
     "handler": tool_endpoint_status},
    {"name": "api_success_rate",
     "description": "For a Response Codes / API Success Rate alert: the matching panels on "
                    "'API Services - Overview' AND the current success rate computed from the "
                    "panel's own Elasticsearch queries. Use this instead of prometheus_query — "
                    "that dashboard is not Prometheus-backed. Pass the alert name verbatim.",
     "inputSchema": {"type": "object", "properties": {
         "alert_name": {"type": "string"}, "window": {"type": "string"}},
         "required": ["alert_name"]},
     "handler": tool_api_success_rate},
    {"name": "render_panel",
     "description": "Render a panel to PNG for attaching to the Slack thread. Pass template "
                    "variables as {\"var-instance\": \"10.60.4.182:9182\"}. Returns an "
                    "evidence_key, or EMPTY if the panel has no data for those variables.",
     "inputSchema": {"type": "object", "properties": {
         "dashboard_uid": {"type": "string"},
         "panel_id": {"type": "integer"},
         "variables": {"type": "object"},
         "from_": {"type": "string", "description": "e.g. now-6h"},
         "to": {"type": "string"},
         "key": {"type": "string", "description": "optional evidence key name"}},
         "required": ["dashboard_uid", "panel_id"]},
     "handler": tool_render_panel},
]

_HANDLERS = {t["name"]: t["handler"] for t in TOOLS}
_TOOL_SPECS = [{k: v for k, v in t.items() if k != "handler"} for t in TOOLS]


# ---------------------------------------------------------------- jsonrpc io

def _result(request_id: Any, payload: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle(request: dict) -> Optional[dict]:
    method, request_id = request.get("method"), request.get("id")
    if method == "initialize":
        return _result(request_id, {"protocolVersion": PROTOCOL_VERSION,
                                    "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO})
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": _TOOL_SPECS})
    if method == "tools/call":
        params = request.get("params") or {}
        handler = _HANDLERS.get(params.get("name"))
        if not handler:
            return _error(request_id, -32602, f"Unknown tool: {params.get('name')}")
        try:
            text = handler(**(params.get("arguments") or {}))
        except Exception as e:                      # noqa: BLE001 — let the model adapt
            return _result(request_id, {"content": [{"type": "text",
                                                     "text": f"{type(e).__name__}: {e}"}],
                                        "isError": True})
        return _result(request_id, {"content": [{"type": "text", "text": text}]})
    if request_id is None:
        return None
    return _error(request_id, -32601, f"Method not found: {method}")


def serve() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        try:
            response = handle(request)
        except Exception as e:                      # noqa: BLE001
            response = _error(request.get("id"), -32603, f"{type(e).__name__}: {e}")
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


def _selftest() -> int:
    for name, args in (("list_datasources", {}),
                       ("search_dashboards", {"query": "memory"}),
                       ("prometheus_query", {"expr": 'windows_os_hostname{hostname="SVC182"}'})):
        try:
            print(f"\n=== {name} ===\n{_HANDLERS[name](**args)[:500]}", file=sys.stderr)
        except Exception as e:                      # noqa: BLE001
            print(f"\n=== {name} FAILED === {type(e).__name__}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    serve()
