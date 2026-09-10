#!/usr/bin/env python3
"""
Thanos, queried directly — for the alerts whose answer is one PromQL expression
and how long it has been true.

The owner pinned this on 2026-08-31 for
    "High concurrent_requests for core-admin-server"
    "High nodejs_active_handles for core-admin-server"
which are investigated by opening https://thanos.ops.veritone.com, running
    concurrent_requests{instance="10.244.188.5:9000"}
and saying what the number is and whether it is still high.

Why direct, when Grafana already proxies Prometheus
---------------------------------------------------
Two reasons, both found by measurement (2026-08-31):

1. THE ALERT RULES ARE HERE. `/api/v1/rules?type=alert` returns the real rule —
   `aiw:zpfc02 - High concurrent_requests for core-admin-server` with query
   `concurrent_requests{env="aiw-zpfc02",job="core-admin-server-service"} > 10`
   and `for: 300`. The alert's own title IS the rule name, so the threshold and
   the exact selector are a lookup rather than a guess. Nothing else in this
   repo had access to that.
2. No dashboard exists for these metrics, so there is no panel to render. The
   graph comes from the Thanos UI itself (which screenshots cleanly headless and
   needs no auth — verified) or from oncall_agent.chart as a fallback.

THE INSTANCE IN THE ALERT GOES STALE. The owner's example
`instance="10.244.188.5:9000"` returned an empty vector: pod IPs churn, and by
the time anyone looks the pod may be gone. That is itself information — the pod
was replaced — so `resolve_series` falls back to the rule's own selector and
says which it used, rather than reporting "no data" for a service that is fine.
"""
import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

BASE_URL = os.environ.get("THANOS_URL", "https://thanos.ops.veritone.com").rstrip("/")
TIMEOUT = int(os.environ.get("THANOS_TIMEOUT", "45"))

# "expr > 10", "expr >= 0.5", "sum(...) > 120" — the comparison a rule ends with.
_COMPARISON = re.compile(r"^(?P<expr>.+?)\s*(?P<op>>=|>|<=|<)\s*(?P<value>[0-9.]+)\s*$", re.S)


class ThanosError(RuntimeError):
    """A failed Thanos call, surfaced as text — never raised into the daemon."""


@dataclass
class AlertRule:
    name: str
    query: str                  # the rule's full expression, comparison included
    expr: str                   # just the metric side, for graphing
    threshold: Optional[float]
    operator: str
    for_seconds: int
    state: str                  # inactive | pending | firing
    labels: Dict[str, str] = field(default_factory=dict)
    group: str = ""


@dataclass
class BreachSummary:
    """Where a metric stands now, and for how long — the "high since 2 hours"
    half of the thread."""
    current: Optional[float]
    minimum: float
    maximum: float
    points: int
    window_hours: int
    above_now: bool
    seconds_in_state: Optional[float]     # None when the whole window is one state
    whole_window: bool
    threshold: Optional[float]

    @property
    def state(self) -> str:
        if self.threshold is None:
            return "no threshold known"
        return "above threshold" if self.above_now else "below threshold"

    def explain(self) -> str:
        """Plain English, the way the on-call engineer would say it."""
        if self.threshold is None:
            return (f"currently {_number(self.current)}; no threshold known, so 'high' cannot "
                    f"be judged from the data alone")
        duration = _duration(self.seconds_in_state)
        if self.above_now and self.whole_window:
            return (f"HIGH — {_number(self.current)} against a threshold of "
                    f"{_number(self.threshold)}, and above it for the whole "
                    f"{self.window_hours}h window (peak {_number(self.maximum)}). It has been "
                    f"high for at least {self.window_hours}h, possibly longer.")
        if self.above_now:
            return (f"HIGH — {_number(self.current)} against a threshold of "
                    f"{_number(self.threshold)}, above it for {duration} "
                    f"(peak {_number(self.maximum)} in the last {self.window_hours}h).")
        if self.maximum > self.threshold:
            return (f"LOW AGAIN — {_number(self.current)} against a threshold of "
                    f"{_number(self.threshold)}. It came back below {duration} ago, after "
                    f"peaking at {_number(self.maximum)} in the last {self.window_hours}h.")
        return (f"LOW — {_number(self.current)} against a threshold of "
                f"{_number(self.threshold)}, and it never reached the threshold in the last "
                f"{self.window_hours}h (peak {_number(self.maximum)}).")


def _number(value: Optional[float]) -> str:
    if value is None:
        return "unknown"
    if abs(value - round(value)) < 0.01:
        return f"{int(round(value))}"
    return f"{value:.2f}"


def _duration(seconds: Optional[float]) -> str:
    if not seconds:
        return "an unknown period"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours}h"


def _get(path: str, params: Optional[dict] = None) -> dict:
    url = f"{BASE_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as e:
        raise ThanosError(f"GET {path} -> HTTP {e.code}: "
                          f"{e.read()[:200].decode('utf-8', 'replace')}") from e
    except Exception as e:                              # noqa: BLE001
        raise ThanosError(f"cannot reach {BASE_URL}: {e}. If Thanos is behind the VPN, that "
                          f"tunnel has to be up on this machine.") from e
    if payload.get("status") != "success":
        raise ThanosError(f"Thanos returned {payload.get('status')}: "
                          f"{str(payload.get('error'))[:200]}")
    return payload.get("data") or {}


def query(expr: str) -> List[dict]:
    """Instant query — the raw series list, labels included."""
    return _get("/api/v1/query", {"query": expr}).get("result") or []


def query_range(expr: str, hours: int = 6, step_seconds: int = 300) -> List[Tuple[float, float]]:
    """One series' worth of (timestamp, value). Wrap the expression in an
    aggregation (max/sum) if it can return several series."""
    end = int(time.time())
    data = _get("/api/v1/query_range", {
        "query": expr, "start": end - hours * 3600, "end": end, "step": step_seconds})
    result = data.get("result") or []
    if not result:
        return []
    return [(float(ts), float(value)) for ts, value in result[0].get("values") or []]


def find_alert_rules(name_fragment: str, limit: int = 5) -> List[AlertRule]:
    """Alerting rules whose NAME contains this fragment.

    The alert title is the rule name (minus the `aiw:<env> - ` prefix
    Alertmanager adds), so this is how the threshold and the exact selector are
    obtained rather than guessed.
    """
    fragment = re.sub(r"^aiw:[a-z0-9-]+\s*-\s*", "", (name_fragment or "").strip(),
                      flags=re.I).lower()
    if not fragment:
        return []
    out: List[AlertRule] = []
    for group in _get("/api/v1/rules", {"type": "alert"}).get("groups") or []:
        for rule in group.get("rules") or []:
            name = rule.get("name") or ""
            if fragment not in name.lower():
                continue
            raw = rule.get("query") or ""
            match = _COMPARISON.match(raw.strip())
            out.append(AlertRule(
                name=name, query=raw,
                expr=(match.group("expr").strip() if match else raw),
                threshold=(float(match.group("value")) if match else None),
                operator=(match.group("op") if match else ""),
                for_seconds=int(rule.get("duration") or 0),
                state=rule.get("state") or "unknown",
                labels=rule.get("labels") or {},
                group=group.get("name") or "",
            ))
    # Longest name first: "High concurrent_requests for core-admin-server" should
    # beat a shorter rule that merely contains the same words.
    out.sort(key=lambda r: -len(r.name))
    return out[:limit]


def resolve_series(expr: str, instance: str = "") -> Tuple[List[dict], str]:
    """Series for this expression, preferring the alert's own instance.

    Returns (series, note). The note is not decoration: when the instance from
    the alert has gone (pod replaced — the owner's example instance returned an
    empty vector), the thread has to say that it fell back to the service rather
    than silently reporting a different scope's numbers.
    """
    if instance:
        scoped = f'{expr}{{instance="{instance}"}}' if "{" not in expr else \
            expr.replace("}", f',instance="{instance}"}}', 1)
        series = query(scoped)
        if series:
            return series, f"scoped to instance {instance}"
        return (query(expr),
                f"instance {instance} from the alert returned NO data — that pod is gone "
                f"(pod IPs churn), so these are the service's current pods instead")
    return query(expr), "all pods of the rule's own selector"


def breach_summary(expr: str, threshold: Optional[float], hours: int = 6,
                   aggregate: str = "max") -> BreachSummary:
    """How long the metric has been on its current side of the threshold."""
    wrapped = f"{aggregate}({expr})"
    points = query_range(wrapped, hours=hours)
    if not points:
        raise ThanosError(f"no datapoints for {wrapped} over the last {hours}h — report that "
                          f"as no data, not as a zero")
    values = [v for _, v in points]
    current = values[-1]
    if threshold is None:
        return BreachSummary(current=current, minimum=min(values), maximum=max(values),
                             points=len(points), window_hours=hours, above_now=False,
                             seconds_in_state=None, whole_window=False, threshold=None)
    above_now = current > threshold
    # Walk back while the state matches; the first differing point is the flip.
    flip_at: Optional[float] = None
    for timestamp, value in reversed(points):
        if (value > threshold) != above_now:
            flip_at = timestamp
            break
    return BreachSummary(
        current=current, minimum=min(values), maximum=max(values), points=len(points),
        window_hours=hours, above_now=above_now,
        seconds_in_state=(time.time() - flip_at) if flip_at else None,
        whole_window=flip_at is None, threshold=threshold)


def graph_url(expr: str, hours: int = 6, step_seconds: int = 300) -> str:
    """The Thanos UI graph link a human would open — and what gets screenshotted."""
    return f"{BASE_URL}/graph?" + urllib.parse.urlencode({
        "g0.expr": expr, "g0.tab": "0", "g0.range_input": f"{hours}h",
        "g0.step_input": str(step_seconds), "g0.max_source_resolution": "0s",
        "g0.deduplicate": "1", "g0.partial_response": "1",
    })


# The Thanos UI graph page, clipped to the query bar + plot. The legend below it
# lists every series with its full label set (7 series x ~10 labels on the real
# core-admin-server rule) and is three times the height of the graph, so it is
# cropped out — the query stays IN frame, because that is the provenance of the
# picture.
_PLOT_BOTTOM_JS = """
() => {
  const canvases = Array.from(document.querySelectorAll('canvas'));
  if (!canvases.length) return null;
  const bottom = Math.max(...canvases.map(c => c.getBoundingClientRect().bottom));
  const labels = Array.from(document.querySelectorAll('.u-legend, [class*=legend]'))
    .map(e => e.getBoundingClientRect().top).filter(v => v > bottom);
  return {bottom: bottom, legendTop: labels.length ? Math.min(...labels) : null};
}
"""


def capture_graph(expr: str, out_path, hours: int = 6, timeout_ms: int = 60000):
    """Screenshot the real Thanos graph for this expression.

    This is the page a human would open, which is why it is preferred over
    drawing the series ourselves: the query is visible in the frame, so the
    reader can see exactly what produced the line. Raises if the plot never
    draws — oncall_agent.chart is the caller's fallback.
    """
    from playwright.sync_api import sync_playwright

    url = graph_url(expr, hours=hours)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_context(viewport={"width": 1400, "height": 900}).new_page()
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            # Wait for a drawn plot, not a timer: the query runs after the page
            # settles, exactly like the Grafana panels.
            page.wait_for_function(
                "() => document.querySelectorAll('canvas').length > 0", timeout=timeout_ms)
            page.wait_for_timeout(2500)
            geometry = page.evaluate(_PLOT_BOTTOM_JS)
            if not geometry:
                raise RuntimeError("the Thanos graph never drew a plot")
            height = min(geometry["bottom"] + 34, 900)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(out_path),
                            clip={"x": 0, "y": 0, "width": 1400, "height": height})
        finally:
            browser.close()
    return out_path
