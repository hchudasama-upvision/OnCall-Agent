import json
from pathlib import Path
from typing import Optional

from ...alert_parser import is_engine_failure_rate
from ...types import ParsedAlert

"""
The master router: deterministic code, not a paid LLM call. Almost every
alert type is unambiguous from its own name ("Engine failure rate...",
"KubePodsNotReady", "DiskSpaceUtilization...") — spending a model call just
to classify what a keyword match already answers correctly would be pure
cost with no accuracy gain. If misrouting ever turns out to be a real
problem for some alert type, replacing this with a small LLM classifier is a
localized swap, not a redesign — route_alert()'s signature stays the same.

This is the "master agent" from the owner's design (2026-08-26): it gets the
alert and decides which specialist agent handles it. It does not itself
gather evidence or post anything — handler.py does the actual "pass the
alert to that agent" part, calling Agents/registry.py's run_specialist()
with whatever name this returns.
"""

_ROUTING_CONFIG = Path(__file__).resolve().parents[4] / "config" / "specialist_routing.json"


def _load_routes() -> list:
    if not _ROUTING_CONFIG.exists():
        return []
    return json.loads(_ROUTING_CONFIG.read_text()).get("routes") or []


def route_alert(alert: ParsedAlert) -> Optional[str]:
    """The specialist name to handle this alert, or None for the generalist
    fallback (case-library + Slack history, no domain tool)."""
    if is_engine_failure_rate(alert):
        return "edge_ui"

    haystack = " ".join([alert.alert_name, alert.incident_name, alert.raw_text]).lower()
    for route in _load_routes():
        keywords = route.get("keywords") or []
        if any(kw.lower() in haystack for kw in keywords):
            return route.get("specialist")
    return None
