import re
from typing import Dict, List, Optional

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

# "key: value" metadata lines. VictorOps emits BOTH cases in the same block:
# its own fields upper-case (ACKED_BY, CURRENT_ALERT_PHASE) and the
# transmitter's lower-case (monitoring_tool, entity_display_name,
# state_message) — and the first of them is glued to the opening ``` fence.
# An upper-case-only pattern silently dropped every lower-case field.
_FIELD_LINE = re.compile(
    r"^[`\s]*\*?([A-Za-z][A-Za-z0-9_ ]{2,40})\*?\s*:\s*(.+?)\s*$", re.MULTILINE
)

# "[FIRING:3] aiw-prd5001 : Engine failure rate above 15% (http://...)"
#
# A card can carry BOTH counts — "[FIRING:16 RESOLVED:5] aiw-uk1001 :
# KubePodsNotReady" — whenever some sub-alerts recovered while others still
# fire, which is the normal state of KubePodsNotReady, NodeHighCPUUsage and the
# organization-failure alerts. The original pattern required "]" immediately
# after the digits, so it matched NONE of those: firing_count came back `0` and
# _alert_name_from fell through to the bare path and returned the ENTIRE line
# ("[FIRING:16 RESOLVED:5] aiw-uk1001 : KubePodsNotReady") as the alertname,
# which then went into the thread heading and the memory key. The trailing
# groups are consumed but deliberately not captured: the FIRING count is the
# one that says how much is still wrong.
_FIRING = re.compile(
    r"\[(FIRING|RESOLVED|CRITICAL|WARNING):?(\d*)"
    r"(?:\s+(?:FIRING|RESOLVED|CRITICAL|WARNING):?\d*)*\]\s*(.*)",
    re.IGNORECASE,
)

_INCIDENT_NUMBER = re.compile(r"[Ii]ncident\s*#\s*(\d+)")
_INCIDENT_UPDATE = re.compile(
    r"[Ii]ncident\s*#?\s*\d+\s*\*?\s*(?:was|is)\s+\*?(ACKED|RESOLVED|RE[- ]?ROUTED|UNACKED)",
    re.IGNORECASE,
)
_ON_CALL_CHANGE = re.compile(r"ON-CALL CHANGE", re.IGNORECASE)

# Slack link syntax leaks into every one of these payloads: <url|label> / <url>.
_SLACK_LINK = re.compile(r"<(https?://[^|>]+)(?:\|([^>]*))?>")

# The VictorOps portal link, and the title that follows it on the same line:
#   *<https://portal.victorops.com/.../incident/119881|Incident #119881>: [FIRING:1] aiw-prd5001 : Engine failure rate above 15%*
# This is where the real title lives. INCIDENT_NAME is NOT it — VictorOps puts
# the incident NUMBER there ("INCIDENT_NAME: 119881"), so reading the title out
# of that field produced threads headed "Alert: 119881".
_VICTOROPS_HEADING = re.compile(
    r"<(?P<url>https?://portal\.victorops\.com/[^|>]*/incident/(?P<number>\d+))\|[^>]*>\s*:\s*(?P<title>[^\n]*)"
)
_VICTOROPS_URL = re.compile(r"https?://portal\.victorops\.com/\S*?/incident/(\d+)")


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


# The three lower-case keys the VictorOps transmitter emits. In a real Slack
# card the first is glued to the ``` fence ("```monitoring_tool: Alertmanager"),
# which _FIELD_LINE's leading [`\s]* already absorbs. In a card PASTED as plain
# text the fence is gone and it glues to whatever ended the title line instead
# ("...above 15%monitoring_tool: Alertmanager"), so the line-anchored regex
# misses it and monitoring_tool comes back empty — which is what tells Runscope,
# Email and NOC-Automation-Script alerts apart from Alertmanager ones.
# Deliberately a fixed list of three, not a general mid-line "word:" rule: a
# general rule would split on every "Summary:" inside prose and on clock times.
_GLUED_KEY = re.compile(
    r"(?<=\S)(?=(?:monitoring_tool|entity_display_name|state_message)\s*:)",
    re.IGNORECASE,
)


def _unglue_transmitter_keys(text: str) -> str:
    """Put a glued transmitter key back on its own line."""
    return _GLUED_KEY.sub("\n", text or "")


def _extract_fields(text: str) -> Dict[str, str]:
    """Upper-case KEY: value pairs, normalized to UPPER_SNAKE keys."""
    fields: Dict[str, str] = {}
    for key, value in _FIELD_LINE.findall(_unglue_transmitter_keys(text)):
        normalized = re.sub(r"\s+", "_", key.strip()).upper()
        fields.setdefault(normalized, _unlink(value).strip())
    return fields


# "<description> (<threshold>) - <HOST>" — the Zabbix/email-sourced shape.
# The host is a single trailing token after a dash, with no spaces in it, so it
# cannot be confused with a hyphenated alert name ("NOC Health Check - Systems
# Alerting" has a space in its tail and is left alone).
_TRAILING_HOST = re.compile(r"^(?P<body>.*?)\s+-\s+(?P<host>[A-Za-z0-9][\w.-]*)\s*$")


def _strip_trailing_host(text: str) -> tuple:
    """(body, host) for the trailing-host shape; (text, "") otherwise."""
    m = _TRAILING_HOST.match(text.strip())
    if not m:
        return text, ""
    return m.group("body"), m.group("host")


def strip_trailing_groups(text: str) -> str:
    """Drop the trailing "(...)" groups an alert line carries.

    Real lines carry up to two: PandoLogic emits the label set AND the
    generator URL — "(pandologic 10.60.4.41:9182 windows_exporter warning
    SQL41 J: windows_exporter) (http://10.60.4.245:9093/#/alerts?...)".
    Stripping only the last one left the label soup in the alert name.
    """
    previous = None
    while previous != text:
        previous = text
        text = re.sub(r"\s*\([^()]*\)\s*$", "", text).strip()
    return text


# A leading segment that names an environment rather than the alert:
# "aiw-prd5001 : …", "us-1 - prod - …", "uk-1 : uk-prod - …", "PandoLogic - …".
_ENV_PREFIX = re.compile(
    r"^(?:aiw-[a-z0-9]+|(?:us|uk|ca|eu|au)-[a-z0-9]+|us-gov-\d+|pandologic|prod|stage|staging|dev|qa)"
    r"\s*[:\-]\s+",
    re.IGNORECASE,
)


def _strip_env_prefixes(text: str) -> str:
    """Peel environment segments off the front, one at a time.

    Splitting on the last " - " instead would be wrong for the alerts whose
    NAME contains one: "[CRITICAL] NOC Health Check - Systems Alerting" is a
    single alertname, and rsplit turned it into "Systems Alerting". Only
    segments that actually look like an environment are removed, so a name
    with a hyphen in it survives intact.
    """
    previous = None
    while previous != text:
        previous = text
        text = _ENV_PREFIX.sub("", text).strip()
    return text


def _alert_name_from(text: str) -> str:
    """The alertname out of "[FIRING:n] <env> <sep> <AlertName> (<links>)".

    Real layouts, all present in #alerts-devops on any given day:
      "[FIRING:1] aiw-prd5001 : Engine failure rate above 15%"
      "[FIRING:1] us-1 - prod - AlbUnhealthyHostWarning"
      "[FIRING:1] uk-1 : uk-prod - ALBUnhealthyHostCritical"
      "[CRITICAL] NOC Health Check - Systems Alerting"   (no env at all)
    """
    m = _FIRING.search(_unlink(text))
    if not m:
        # Not an Alertmanager line. Zabbix/email-sourced incidents reach
        # VictorOps in a different shape entirely — no [FIRING:n], no label
        # group, and the HOST trailing after a dash:
        #     "High memory utilization (>95% for 5m) - SVC183"
        # The parenthesis here is a threshold, not labels.
        bare = strip_trailing_groups(_strip_trailing_host(_unlink(text))[0].strip())
        return bare.strip().rstrip("*").strip()
    rest = strip_trailing_groups(m.group(3).strip())
    # The heading line is wrapped in Slack bold, so the tail carries a "*".
    rest = rest.strip().rstrip("*").strip()
    return _strip_env_prefixes(rest)


# Alertmanager's Slack template renders the alert's LABEL VALUES as a
# space-separated list inside the trailing parentheses, e.g.
#   "(pandologic 10.60.4.41:9182 windows_exporter warning SQL41 J: windows_exporter)"
#   "(n/a triggeredAlarm:VmCPUUsageAlarm RealMatch-Cluster02 ... STG-Backend5)"
# The label NAMES are not transmitted, only the values — so they are matched by
# shape, not by key. That is lossy but it is all the payload carries, and it is
# what turns an alert into a Grafana template variable (which host? which VM?).
_LABEL_GROUP = re.compile(r"\(([^()]*)\)")

_HOSTPORT = re.compile(r"^(?P<host>[\w.-]+):(?P<port>\d+)$")
_WINDOWS_VOLUME = re.compile(r"^[A-Z]:$")
_KNOWN_NOISE = {"n/a", "none", "null", "warning", "critical", "info", "pandologic"}


def extract_labels(text: str) -> List[str]:
    """Label values from the alert's parenthesised groups, URLs excluded.

    Identical groups are collapsed. A VictorOps card restates the same alert
    line up to four times (linked heading, plain body, entity_display_name and
    SERVICE), so without this the label list repeats 4x and every {label:N}
    index a human picked off the output would be off by a whole period.
    Deduplication is per GROUP, not per value — inside one group a value can
    legitimately repeat ("... windows_exporter warning SQL41 J: windows_exporter").
    """
    values: List[str] = []
    seen_groups = set()
    for group in _LABEL_GROUP.findall(_unlink(text)):
        normalized = " ".join(group.split())
        if not normalized or normalized.lower().startswith(("http", "see ", "runbook")):
            continue
        if normalized in seen_groups:
            continue
        seen_groups.add(normalized)
        values.extend(normalized.split())
    return values


def label_hints(values: List[str]) -> Dict[str, str]:
    """Only the label values whose SHAPE identifies them beyond doubt.

    Deliberately narrow. An earlier version also guessed the host/VM name as
    "the last non-noise value", which is right for
    "... vmware_vcenter critical n/a STG-Backend5" and wrong for
    "... warning SQL41 J: windows_exporter" — it returned the job name as the
    host. A wrong hint renders a real-looking graph of the wrong machine,
    which is worse than rendering none, so anything ambiguous is left out and
    the panel entry names an explicit {label:N} index instead.
    """
    hints: Dict[str, str] = {}
    for value in values:
        hostport = _HOSTPORT.match(value)
        if hostport and "instance" not in hints:
            hints["instance"] = value                       # "10.60.4.41:9182"
            hints.setdefault("ip", hostport.group("host"))
        elif _WINDOWS_VOLUME.match(value) and "volume" not in hints:
            hints["volume"] = value                         # "J:"
        elif value.lower().endswith((".com", ".net", ".local")) and "fqdn" not in hints:
            hints["fqdn"] = value                           # "ny1esx9680.verimatch.com"
    return hints


def _first(fields: Dict[str, str], *keys: str) -> str:
    for key in keys:
        if fields.get(key):
            return fields[key]
    return ""


def extract_environment_key(text: str) -> Optional[str]:
    """Pulls the "aiw-xxx" environment key out of a VictorOps incident title/
    entity name. Generic across every alert domain (any alert can name an
    environment, not just engine-failure ones) — agents/edge_ui/ re-uses this
    same function rather than a domain-local copy."""
    match = re.search(r"\baiw-[a-z0-9]+\b", text, re.IGNORECASE)
    return match.group(0).lower() if match else None


def parse_alert_message(message: dict, channel_id: str = "", permalink: str = "") -> ParsedAlert:
    """One #alerts-devops Slack message -> ParsedAlert. Never raises on content."""
    raw = flatten_message(message)
    flat = _unlink(raw)
    fields = _extract_fields(raw)

    # The VictorOps heading is authoritative for all three of number, title
    # and link — the metadata fields below are not (INCIDENT_NAME holds the
    # number, and entity_display_name repeats the raw firing line).
    heading = _VICTOROPS_HEADING.search(raw)
    incident_url = ""
    incident_number = 0
    incident_name = ""
    if heading:
        incident_number = int(heading.group("number"))
        incident_url = heading.group("url")
        incident_name = heading.group("title").strip().strip("*").strip()
    else:
        url_only = _VICTOROPS_URL.search(raw)
        if url_only:
            incident_url = url_only.group(0)
            incident_number = int(url_only.group(1))

    if not incident_number:
        number_match = _INCIDENT_NUMBER.search(flat)
        incident_number = int(number_match.group(1)) if number_match else 0

    firing = _FIRING.search(flat)
    firing_count = int(firing.group(2)) if (firing and firing.group(2)) else 0

    entity_display_name = _first(fields, "ENTITY_DISPLAY_NAME", "SERVICE", "ENTITY_ID", "HOST")
    if not incident_name:
        # Alertmanager posts have no heading — their first line is the title,
        # minus the trailing "(...)" groups that read as noise in a header.
        first_line = next((line.strip() for line in flat.splitlines() if line.strip()), "")
        # A card PASTED as plain text keeps the words "Incident #121608: " on
        # that first line, because the linked-heading form the regex above
        # looks for ("*<url|Incident #N>: title*") is flattened away when a
        # human copies it. Left in, it is emitted a SECOND time by
        # compose_top_level_text, which adds its own reference — the header
        # read "Incident #121608: Incident #121608: [FIRING:1] ...". The
        # number is already captured separately, so strip the prefix.
        first_line = re.sub(r"^\*?\s*Incident\s*#\d+\s*:\s*", "", first_line).strip()
        incident_name = strip_trailing_groups(first_line) or first_line

    # Prefer the heading title: it is already unwrapped and free of the
    # metadata block, so the firing-line regex cannot drift onto another line.
    alert_name = (_alert_name_from(incident_name) if incident_name else "") \
        or _alert_name_from(flat) or _first(fields, "ALERTNAME", "ALERT_NAME")
    env_key = extract_environment_key(flat) or ""

    # The state_message block, when the card carries one. Parsed BEFORE the
    # positional labels because it changes where those may legitimately come
    # from: the Summary lines contain parentheses of their own ("High CPU:
    # ... (us-east-1c)"), and extract_labels read those availability zones as
    # if they were Alertmanager label values. Measured on incident #121477.
    sub_alerts = parse_state_message(raw)
    label_source = raw
    if sub_alerts:
        span = state_message_span(raw)
        if span:
            label_source = raw[:span[0]] + " " + raw[span[1]:]

    labels = extract_labels(label_source)
    # Named labels first: `dbinstance_identifier`, `node`, `pod`,
    # `load_balancer` are transmitted by NAME here, so they are facts rather
    # than the shape-based guesses label_hints() has to make from a bare list
    # of values. The guesses stay as the fallback for cards with no block.
    hints = named_label_hints(sub_alerts)
    for key, value in label_hints(labels).items():
        hints.setdefault(key, value)
    if env_key:
        hints.setdefault("env", env_key)
    if not _FIRING.search(flat) and incident_name:
        # Only for the non-Alertmanager shape: on an Alertmanager line the
        # trailing token is part of the alertname, not a host. Read it off the
        # parsed TITLE — the first line of a VictorOps card is
        # "*Organization:* wazee-digital-inc", not the alert.
        _, trailing_host = _strip_trailing_host(strip_trailing_groups(incident_name))
        if trailing_host:
            hints.setdefault("host", trailing_host)

    source, kind = _classify(flat, raw, fields, incident_number, bool(heading))

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
        incident_url=incident_url,
        alert_phase=_first(fields, "CURRENT_ALERT_PHASE"),
        alert_state=_first(fields, "CURRENT_STATE"),
        acked_by=_first(fields, "ACKED_BY"),
        # MONITOR_TYPE is VictorOps' own transport label ("API", "UNKNOWN");
        # monitoring_tool is the real source ("Alertmanager", "NOC Automation
        # Script"), so it has to win.
        monitoring_tool=_first(fields, "MONITORING_TOOL", "MONITOR_TYPE"),
        # The FULL block, not the first line _extract_fields could see — the
        # Summary/Description/Labels below it were being dropped silently.
        state_message=(state_message_body(raw)
                       or _first(fields, "STATE_MESSAGE", "MESSAGE", "DESCRIPTION", "SUMMARY")),
        escalation_policy=_first(fields, "ESCALATION_POLICY", "CONTACTGROUPNAME",
                                 "ROUTING_KEY", "PAGING_POLICY"),
        alert_name=alert_name,
        environment_key=env_key,
        firing_count=firing_count,
        author_id=(message.get("bot_id") or message.get("user") or ""),
        labels=labels,
        label_hints=hints,
        fields=fields,
        sub_alerts=sub_alerts,
    )


def _classify(flat: str, raw: str, fields: Dict[str, str], incident_number: int,
              has_victorops_heading: bool = False) -> tuple:
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

    if has_victorops_heading or incident_number:
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
        # The VictorOps portal URL is what a human links in #comms-noc; the
        # Slack permalink of the alert card is only the fallback.
        slack_permalink=alert.incident_url or alert.permalink,
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


# --------------------------------------------------------- the state_message
# body: Summary / Description / Labels, per sub-alert
#
# Added 2026-09-12 after scanning the real channel. The Alertmanager ->
# VictorOps template gained this block somewhere between 2026-08-24 and
# 2026-09-11: the cards in fixtures/alerts-devops-real.json carry
# "state_message: *Alertmanager:* <url>" and nothing else, while every
# Alertmanager card in the channel today carries one block PER SUB-ALERT:
#
#     state_message: &bull; *[RESOLVED] NodeHighCPUUsage*
#       *Summary:* High CPU: engine_segregated node in aiw-prod1001 (us-east-1c)
#       *Description:* Node ip-10-0-100-23.ec2.internal [...] VALUE = 96.188
#       *Runbook:* <https://...>            (engine-failure alerts only)
#       *Labels:*
#         - env: aiw-prod1001
#         - node: ip-10-0-100-23.ec2.internal
#         ...
#       *Started:* 2026-09-11 10:19:47 UTC
#       *Resolved:* 2026-09-11 10:31:47 UTC
#     &bull; *[FIRING] NodeHighCPUUsage*
#       ...
#     *Alertmanager:* <http://thanos-alertmanager.ops.veritone.com>
#
# This matters more than it looks. The LABEL NAMES are transmitted here —
# `dbinstance_identifier`, `node`, `pod`, `load_balancer`, `engineName` — while
# extract_labels() above only ever sees positional label VALUES in the
# parentheses of an Alertmanager-direct post and has to guess at their meaning
# by shape. A named label is not a guess, so when this block is present it is
# the better source and label_hints prefers it.
#
# Three traps, all hit on real payloads:
#  1. The bullet arrives HTML-ESCAPED as "&bull;", not "•".
#  2. _extract_fields() is line-based, so STATE_MESSAGE captured only the FIRST
#     LINE and the whole block below it was silently dropped.
#  3. The card is truncated mid-block when it carries many sub-alerts, so the
#     last block can end anywhere — after "*Labels:*" with no labels, or
#     halfway through a value. A partial block is kept and flagged, never
#     discarded and never completed by guessing.
_SUB_ALERT_SPLIT = re.compile(r"(?:&bull;|•)\s*\*\[", re.IGNORECASE)
_SUB_ALERT_HEAD = re.compile(r"^(FIRING|RESOLVED)\]\s*\*?(.*?)\*?\s*$",
                             re.IGNORECASE | re.MULTILINE)
_SUB_FIELD = re.compile(r"^\s*\*(Summary|Description|Runbook|Started|Resolved)\s*:\*\s*(.*?)\s*$",
                        re.MULTILINE)
_SUB_LABEL = re.compile(r"^\s*-\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$", re.MULTILINE)
# Where the per-sub-alert blocks stop and VictorOps' own metadata begins.
_STATE_MESSAGE_END = re.compile(
    r"^\s*(?:\*Alertmanager:\*|[A-Z][A-Z0-9_]{2,}\s*:)", re.MULTILINE
)


def state_message_span(raw_text: str) -> Optional[tuple]:
    """(start, end) of the state_message body inside the RAW text, or None.

    Kept separate from state_message_body because the body is normalized for
    display and the span is not: callers that need to EXCLUDE this region from
    some other scan (extract_labels does) must slice the raw string, or the
    normalization makes the two disagree and the exclusion silently no-ops.
    That is a bug this function exists to have already fixed once.
    """
    # NOT anchored to the start of a line, and NOT un-glued first: the span is
    # sliced out of the ORIGINAL string by the caller, so inserting newlines
    # here would shift every offset past the insertion point and the slice
    # would come back misaligned. (Exactly the raw-vs-normalized mismatch this
    # function was created to prevent — reintroduced once while fixing pasted
    # cards, caught by test_state_message_sub_alerts.)
    match = re.search(r"state_message\s*:[ \t]*(.*)$", raw_text or "",
                      re.MULTILINE | re.IGNORECASE)
    if not match:
        return None
    start = match.start(1)
    rest = (raw_text or "")[start:]
    end = _STATE_MESSAGE_END.search(rest)
    return (start, start + (end.start() if end else len(rest)))


def state_message_body(raw_text: str) -> str:
    """The FULL state_message, not just its first line.

    `_extract_fields` is line-based by design (the metadata block really is
    one KEY: value per line), so it cannot carry this. Bounded at the first
    VictorOps metadata key or the "*Alertmanager:*" footer, so none of that
    leaks into the body.
    """
    span = state_message_span(raw_text)
    if not span:
        return ""
    body = (raw_text or "")[span[0]:span[1]].strip().rstrip("`").strip()
    # Cosmetic only, and deliberately just this one entity: the bullet arrives
    # as "&bull;" and reads as noise in the prompt. Nothing else is unescaped
    # here — an entity inside a URL or an evidence key must stay exactly as the
    # payload had it, so the fabrication guardrail keeps its exact-match
    # property (non-negotiable #2).
    return body.replace("&bull;", "\u2022")


def parse_state_message(raw_text: str) -> List[dict]:
    """One dict per sub-alert in the card's state_message.

    Returns [] for the shapes that have no such block — the Zabbix/Runscope/
    NOC-Health-Check cards, and any Alertmanager card whose state_message
    VictorOps has replaced with "Automatically resolved" once the incident
    closed (that replacement is real and common: the rich block is only
    present while the incident is live).
    """
    body = state_message_body(raw_text)
    if not body:
        return []
    chunks = _SUB_ALERT_SPLIT.split(body)
    if len(chunks) < 2:                     # no bullet -> not this shape
        return []

    out: List[dict] = []
    for chunk in chunks[1:]:
        head = _SUB_ALERT_HEAD.search(chunk)
        if not head:
            continue
        sub = {
            "status": head.group(1).upper(),
            "alertname": _unlink(head.group(2)).strip(),
            "summary": "",
            "description": "",
            "runbook": "",
            "started": "",
            "resolved": "",
            "labels": {},
            "truncated": False,
        }
        rest = chunk[head.end():]
        for key, value in _SUB_FIELD.findall(rest):
            sub.setdefault(key.lower(), "")
            if not sub[key.lower()]:
                sub[key.lower()] = _unlink(value).strip()
        labels_at = re.search(r"^\s*\*Labels:\*\s*$", rest, re.MULTILINE)
        if labels_at:
            label_block = rest[labels_at.end():]
            stop = re.search(r"^\s*\*(?:Started|Resolved|Summary|Description|Runbook):\*",
                             label_block, re.MULTILINE)
            if stop:
                label_block = label_block[:stop.start()]
            for name, value in _SUB_LABEL.findall(label_block):
                sub["labels"].setdefault(name, _unlink(value).strip())
        # A block cut off mid-render. EVERY complete block observed carries a
        # *Started:* line — it is the last thing before the next bullet — so a
        # block with a summary and no Started is a tail that got cut, even when
        # its labels made it through intact. That case is real: a card pasted by
        # hand ends wherever the person stopped copying, and the final block had
        # a full label list and no Started. Without the flag the model is free
        # to report that resource as having no start time, which is a fact the
        # payload never carried.
        if labels_at and not sub["labels"]:
            sub["truncated"] = True
        if sub["summary"] and not sub["started"]:
            sub["truncated"] = True
        out.append(sub)
    return out


def named_label_hints(sub_alerts: List[dict]) -> Dict[str, str]:
    """Named labels from the sub-alerts, a FIRING one winning.

    Firing first and not merely first-listed: a card routinely mixes both
    ("[FIRING:1 RESOLVED:2]") and VictorOps renders the RESOLVED ones at the
    top, so taking the first block made `node` the machine that recovered 40
    minutes ago rather than the one still above threshold — measured on
    incident #121477, where the resolved block names ip-10-0-100-23 and the
    firing block names ip-10-0-41-41. A hint that points at the recovered
    resource sends the investigation at the wrong machine, which is the same
    class of bug as a wrong panel id.

    Where several firing sub-alerts disagree (9 nodes on one card), the count
    is itself the finding and `sub_alerts` still carries every one of them.
    """
    firing = [s for s in sub_alerts if str(s.get("status", "")).upper() == "FIRING"]
    hints: Dict[str, str] = {}
    for sub in (firing or sub_alerts):
        for name, value in (sub.get("labels") or {}).items():
            if value and name not in hints:
                hints[name] = value
    return hints
