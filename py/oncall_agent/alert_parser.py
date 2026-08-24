import re
from typing import Dict, List, Optional

from .edge_environments import extract_environment_key
from .types import ParsedAlert, VictorOpsIncident

"""
Parses the five bot formats seen in #alerts-devops (DESIGN.md §1.1) into one
normalized ParsedAlert.

Written defensively on purpose: these are third-party integration payloads
that change without notice, and a parser that raises takes the listener down
with it. Every extraction degrades to "" / 0 and the raw text is always
preserved, so an unrecognized format still reaches triage as text rather
than being dropped.

Slack delivers this content across three places depending on the
integration — top-level `text`, `attachments[].{title,text,fields}`, and
`blocks[]` — so flatten_message() walks all three before any regex runs.
"""

# "KEY: value" lines, as VictorOps/Splunk On-Call renders incident metadata.
_FIELD_LINE = re.compile(r"^\s*\*?([A-Z][A-Z0-9_ ]{2,40})\*?\s*:\s*(.+?)\s*$", re.MULTILINE)

# "[FIRING:3] aiw-prd5001 - Engine failure rate above 15% (http://...)"
_FIRING = re.compile(r"\[(FIRING|RESOLVED):(\d+)\]\s*(.*)", re.IGNORECASE)

_INCIDENT_NUMBER = re.compile(r"[Ii]ncident\s*#\s*(\d+)")
_INCIDENT_UPDATE = re.compile(
    r"[Ii]ncident\s*#\s*\d+\s+was\s+(ACKED|RESOLVED|RE[- ]?ROUTED|UNACKED)", re.IGNORECASE
)
_ON_CALL_CHANGE = re.compile(r"ON-CALL CHANGE", re.IGNORECASE)

# Slack link syntax leaks into every one of these payloads: <url|label> / <url>.
_SLACK_LINK = re.compile(r"<(https?://[^|>]+)(?:\|([^>]*))?>")


def _unlink(text: str) -> str:
    """Render Slack's <url|label> as its label (or the bare url) for matching."""
    return _SLACK_LINK.sub(lambda m: m.group(2) or m.group(1), text or "")


def flatten_message(message: dict) -> str:
    """All human-readable text of a Slack message: text + attachments + blocks."""
    parts: List[str] = [message.get("text") or ""]

    for att in message.get("attachments") or []:
        parts += [att.get("title") or "", att.get("pretext") or "", att.get("text") or ""]
        for fld in att.get("fields") or []:
            title, value = fld.get("title") or "", fld.get("value") or ""
            parts.append(f"{title}: {value}" if title else value)

    def walk_block(block: dict) -> None:
        text = block.get("text")
        if isinstance(text, dict):
            parts.append(text.get("text") or "")
        elif isinstance(text, str):
            parts.append(text)
        for fld in block.get("fields") or []:
            if isinstance(fld, dict):
                parts.append(fld.get("text") or "")
        for el in block.get("elements") or []:
            if isinstance(el, dict):
                walk_block(el)

    for block in message.get("blocks") or []:
        walk_block(block)

    return "\n".join(p for p in parts if p).strip()


def _extract_fields(text: str) -> Dict[str, str]:
    """Upper-case KEY: value pairs, normalized to UPPER_SNAKE keys."""
    fields: Dict[str, str] = {}
    for key, value in _FIELD_LINE.findall(text):
        normalized = re.sub(r"\s+", "_", key.strip()).upper()
        fields.setdefault(normalized, _unlink(value).strip())
    return fields


def _alert_name_from(text: str) -> str:
    """The Alertmanager alertname out of "[FIRING:n] <env> - <AlertName> (<link>)".

    Both the "<env> - <name>" and bare "<name>" layouts appear in the
    channel, and the trailing "(...)" is the runbook/generator link, which is
    never part of the name.
    """
    m = _FIRING.search(_unlink(text))
    if not m:
        return ""
    rest = m.group(3).strip()
    rest = re.sub(r"\s*\((?:https?://|see |runbook)[^)]*\)\s*$", "", rest, flags=re.IGNORECASE).strip()
    rest = re.sub(r"\s*\([^)]*\)\s*$", "", rest).strip()
    if " - " in rest:
        rest = rest.split(" - ", 1)[1].strip()
    return rest


def _first(fields: Dict[str, str], *keys: str) -> str:
    for key in keys:
        if fields.get(key):
            return fields[key]
    return ""


def parse_alert_message(message: dict, channel_id: str = "", permalink: str = "") -> ParsedAlert:
    """One #alerts-devops Slack message -> ParsedAlert. Never raises on content."""
    raw = flatten_message(message)
    flat = _unlink(raw)
    fields = _extract_fields(raw)

    incident_match = _INCIDENT_NUMBER.search(flat)
    incident_number = int(incident_match.group(1)) if incident_match else 0

    firing = _FIRING.search(flat)
    firing_count = int(firing.group(2)) if firing else 0

    incident_name = _first(fields, "INCIDENT_NAME", "ENTITY_DISPLAY_NAME", "ALERT")
    entity_display_name = _first(fields, "ENTITY_DISPLAY_NAME", "ENTITY_ID", "HOST")
    if not incident_name:
        # Alertmanager posts carry no INCIDENT_NAME — the first line is the
        # title, minus the trailing "(<generator link>)" that every one of
        # them carries and that reads as noise in a #comms-noc header.
        first_line = next((line.strip() for line in flat.splitlines() if line.strip()), "")
        incident_name = re.sub(r"\s*\([^)]*\)\s*$", "", first_line).strip() or first_line

    alert_name = _alert_name_from(flat) or _first(fields, "ALERTNAME", "ALERT_NAME")
    env_key = extract_environment_key(flat) or ""

    source, kind = _classify(flat, raw, fields, incident_number)

    return ParsedAlert(
        source=source,
        kind=kind,
        raw_text=raw,
        channel_id=channel_id or message.get("channel", "") or "",
        message_ts=message.get("ts", "") or "",
        permalink=permalink,
        incident_number=incident_number,
        incident_name=incident_name,
        entity_display_name=entity_display_name,
        monitoring_tool=_first(fields, "MONITORING_TOOL", "MONITOR_TYPE"),
        state_message=_first(fields, "STATE_MESSAGE", "MESSAGE", "DESCRIPTION", "SUMMARY"),
        escalation_policy=_first(fields, "ESCALATION_POLICY", "ROUTING_KEY", "PAGING_POLICY"),
        alert_name=alert_name,
        environment_key=env_key,
        firing_count=firing_count,
        fields=fields,
    )


def _classify(flat: str, raw: str, fields: Dict[str, str], incident_number: int) -> tuple:
    """(source, kind). Only (victorops, incident) is a trigger — DESIGN.md v1.1.

    Both the unlinked and raw text are inspected: PandoLogic's only reliable
    tell is its local Alertmanager host in the generator URL, and _unlink()
    replaces that URL with its link label, hiding it.
    """
    lowered = flat.lower()

    if _ON_CALL_CHANGE.search(flat):
        return "oncall_rotation", "rotation"

    if _INCIDENT_UPDATE.search(flat):
        # "Incident #N was ACKED/RESOLVED …" — threaded state changes. Real
        # signal (they close the loop) but never a fresh investigation.
        return "victorops", "incident_update"

    if incident_number:
        return "victorops", "incident"

    if "smoke test" in lowered or "jenkins" in lowered:
        return "jenkins", "report"

    if "pandologic" in lowered or "10.60.4.245" in raw or "10.60.4.245" in flat:
        return "pandologic", "warning"

    if _FIRING.search(flat) or fields.get("ALERTNAME"):
        return "alertmanager", "warning"

    return "unknown", "unknown"


def is_engine_failure_rate(alert: ParsedAlert) -> bool:
    """True for the one alert type this repo already automates end to end.

    Engine-failure incidents route to the deterministic Edge UI pipeline
    (real task stats, real screenshots, real logs). Everything else goes to
    the generic case-library + Grafana triage path.
    """
    haystack = " ".join([alert.incident_name, alert.alert_name, alert.state_message,
                         alert.entity_display_name, alert.raw_text]).lower()
    return bool(re.search(r"engine\s+failure\s+rate", haystack))


def to_victorops_incident(alert: ParsedAlert, organization: str = "wazee-digital-inc") -> VictorOpsIncident:
    """ParsedAlert -> the incident shape the existing engine-failure pipeline takes.

    entity_display_name is internal matching text only (never rendered): the
    incident line plus whatever entity the card carried is what lets
    extract_environment_key and the engine selector find both the aiw-xxx env
    and the engine name without any per-alert code — same contract as the
    manual CLI path in scripts/run_live_test.py.
    """
    return VictorOpsIncident(
        incident_number=alert.incident_number,
        organization=organization,
        incident_name=alert.incident_name,
        entity_display_name=f"{alert.incident_name} {alert.entity_display_name}".strip(),
        monitoring_tool=alert.monitoring_tool or "Alertmanager",
        state_message=alert.state_message,
        escalation_policy=alert.escalation_policy or "NOC-VT OnCall",
        slack_permalink=alert.permalink,
        created_at=alert.message_ts,
    )


def window_minutes_from(alert: ParsedAlert, default: int = 15) -> int:
    """The "over the last N minutes" the alert itself states, when it states one."""
    m = re.search(r"last\s+(\d+)\s*(hour|minute)s?",
                  f"{alert.state_message} {alert.incident_name} {alert.raw_text}", re.IGNORECASE)
    if not m:
        return default
    amount = int(m.group(1))
    return amount * 60 if m.group(2).lower() == "hour" else amount
