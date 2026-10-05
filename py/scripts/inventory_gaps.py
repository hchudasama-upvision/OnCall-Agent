#!/usr/bin/env python3
"""
What the live channel carries that the case inventory does not yet know.

    python py/scripts/inventory_gaps.py --alerts scan.txt
    python py/scripts/inventory_gaps.py --alerts scan.txt --comms comms.txt --json

This is the deterministic half of the `daily-update` skill. The skill (run by
Claude Code, which has the Slack connector) saves the connector's output
VERBATIM to a file; this script parses those cards with the agent's own
alert_parser and reports what is missing. Nothing here calls Slack, and nothing
here edits a case file — it only says what a human (or the skill) should look at.

Why verbatim-file-in rather than a model summarising the channel: a summary of a
payload is a paraphrase, and a paraphrased label name silently becomes a
fabricated one. The cards go to disk exactly as Slack returned them and the
parser reads them, so every finding below traces to real bytes.

Input format is the connector's own dump — blocks that look like:

    === Message from VictorOps (BCT64JZ16) at 2026-09-11 15:49:24 IST ===
    Message TS: 1789121964.747289
    <blank>
    Attachment: *Organization:* wazee-digital-inc
    ...

A JSON array (of strings, or of {"text": ...} objects) is accepted too, so a
future non-connector source needs no new format.
"""
import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import bootstrap  # noqa: E402

bootstrap()

from oncall_agent import case_library                      # noqa: E402
from oncall_agent.Agents.MASTER_Agent.router import route_alert  # noqa: E402
from oncall_agent.alert_parser import parse_alert_message  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]

# Every specialist's own case file. Kept as a list rather than importing the
# prompt modules, so this script runs with no MCP servers and no credentials.
CASE_FILES = {
    "edge_ui": "py/oncall_agent/Agents/Edgeui_Agent/data/cases.json",
    "kubernetes": "py/oncall_agent/Agents/K8S_Agent/data/cases.json",
    "grafana_metrics": "py/oncall_agent/Agents/Grafana_Agent/grafana_metrics/data/cases.json",
    "runscope": "py/oncall_agent/Agents/Runscope_Agent/data/cases.json",
    "aws": "py/oncall_agent/Agents/AWS_Agent/data/cases.json",
}

_BLOCK = re.compile(r"^=== Message from .*?===\s*$", re.MULTILINE)
_TS_LINE = re.compile(r"^Message TS:\s*([0-9.]+)\s*$", re.MULTILINE)


def _split_connector_dump(text: str) -> list:
    """The connector's message blocks -> [{"text": ..., "ts": ...}]."""
    out = []
    parts = _BLOCK.split(text)
    for part in parts[1:] if len(parts) > 1 else []:
        ts_match = _TS_LINE.search(part)
        body = _TS_LINE.sub("", part).strip()
        # "Attachment:" is the connector's own label, not part of the card.
        body = re.sub(r"^Attachment:\s*", "", body, flags=re.MULTILINE)
        if body:
            out.append({"text": body, "ts": ts_match.group(1) if ts_match else ""})
    return out


def load_messages(path: Path) -> list:
    raw = path.read_text()
    stripped = raw.lstrip()
    if stripped.startswith("[") or stripped.startswith("{"):
        data = json.loads(raw)
        if isinstance(data, dict):
            data = data.get("messages", [])
        return [{"text": m, "ts": ""} if isinstance(m, str) else m for m in data]
    return _split_connector_dump(raw)


def _documented_labels(case: dict) -> set:
    """Label names an alert_payload block already mentions."""
    payload = case.get("alert_payload") or {}
    names = set()
    for entry in payload.get("labels") or []:
        # Entries read "instance (<ip>:9100, node-exporter)" — the name is the
        # first token, the rest is the note explaining it.
        name = str(entry).split("(")[0].strip()
        if name:
            names.add(name)
    return names


def _minutes_between(started: str, resolved: str):
    fmt = "%Y-%m-%d %H:%M:%S UTC"
    try:
        return round((datetime.strptime(resolved, fmt)
                      - datetime.strptime(started, fmt)).total_seconds() / 60)
    except (ValueError, TypeError):
        return None


def analyse(messages: list) -> dict:
    libs = {name: case_library.load_case_library(_ROOT / path)
            for name, path in CASE_FILES.items()}
    raw_cases = {name: json.loads((_ROOT / path).read_text())
                 for name, path in CASE_FILES.items()}

    seen = defaultdict(lambda: {
        "count": 0, "incidents": [], "labels": Counter(), "durations": [],
        "with_block": 0, "summaries": [], "monitoring_tools": Counter(),
    })

    for message in messages:
        alert = parse_alert_message({"text": message.get("text", ""),
                                     "ts": message.get("ts", "")})
        if not alert.alert_name:
            continue
        rec = seen[alert.alert_name]
        rec["count"] += 1
        if alert.incident_number and alert.incident_number not in rec["incidents"]:
            rec["incidents"].append(alert.incident_number)
        if alert.monitoring_tool:
            rec["monitoring_tools"][alert.monitoring_tool] += 1
        if alert.sub_alerts:
            rec["with_block"] += 1
        for sub in alert.sub_alerts:
            for label in (sub.get("labels") or {}):
                rec["labels"][label] += 1
            if sub.get("summary") and len(rec["summaries"]) < 3:
                rec["summaries"].append(sub["summary"])
            minutes = _minutes_between(sub.get("started", ""), sub.get("resolved", ""))
            if minutes is not None:
                rec["durations"].append(minutes)

        rec["specialist"] = str(route_alert(alert))

    findings = []
    for name, rec in sorted(seen.items(), key=lambda kv: -kv[1]["count"]):
        specialist = rec.get("specialist", "")
        lib = libs.get(specialist)
        cases = lib.find(lib.fingerprint(name)) if lib else []
        case = cases[0] if cases else None
        documented = _documented_labels(case) if case else set()
        observed = set(rec["labels"])

        issues = []
        if specialist not in libs:
            issues.append("UNROUTED — falls through to the generalist; no specialist owns it")
        if not case:
            issues.append(f"NO CASE — routed to {specialist!r} but no case entry matches")
        elif "alert_payload" not in case:
            if rec["with_block"]:
                issues.append("NO alert_payload BLOCK — but its cards DO carry "
                              "Summary/Description/Labels; the fields are going unused")
        else:
            new_labels = sorted(observed - documented)
            if new_labels:
                issues.append("UNDOCUMENTED LABELS: " + ", ".join(new_labels))
        if case and rec["durations"]:
            observed_median = sorted(rec["durations"])[len(rec["durations"]) // 2]
            documented_minutes = case.get("typical_resolution_minutes")
            if isinstance(documented_minutes, (int, float)) and documented_minutes:
                ratio = observed_median / documented_minutes if documented_minutes else 0
                if ratio and (ratio > 2 or ratio < 0.5):
                    issues.append(
                        f"RESOLUTION TIME DRIFT: observed median {observed_median}m vs "
                        f"documented {documented_minutes}m")

        findings.append({
            "alert_name": name,
            "count": rec["count"],
            "specialist": specialist,
            "case": case.get("fingerprint") if case else None,
            "has_payload_block": bool(case and "alert_payload" in case),
            "cards_with_block": rec["with_block"],
            "observed_labels": sorted(observed),
            "undocumented_labels": sorted(observed - documented) if case else sorted(observed),
            "monitoring_tools": sorted(rec["monitoring_tools"]),
            "sample_incidents": rec["incidents"][:5],
            "sample_summaries": rec["summaries"],
            "observed_resolution_minutes": sorted(rec["durations"])[:10],
            "issues": issues,
        })
    return {"alert_types": findings, "messages_parsed": len(messages)}


_COMMS_ALERT = re.compile(r"Incident #(\d+)\s*[:>]?\s*(.*)")


def analyse_comms(text: str) -> list:
    """Threads in #comms-noc, as (incident, alert line, reply count).

    Deliberately shallow: what the on-call engineer actually DID is prose, and
    judging whether it differs from the case file is the model's job, not a
    regex's. This only finds the threads worth reading.
    """
    out = []
    for block in _split_connector_dump(text):
        match = _COMMS_ALERT.search(block["text"])
        if not match:
            continue
        replies = re.search(r"Thread:\s*(\d+)\s*repl", block["text"])
        out.append({
            "incident": int(match.group(1)),
            "line": match.group(2).strip().strip("*").strip(),
            "replies": int(replies.group(1)) if replies else 0,
            "ts": block["ts"],
        })
    return out


def report(result: dict, comms: list) -> str:
    lines = [f"# Inventory gaps — {result['messages_parsed']} message(s) parsed", ""]
    flagged = [f for f in result["alert_types"] if f["issues"]]
    clean = [f for f in result["alert_types"] if not f["issues"]]

    lines.append(f"{len(flagged)} alert type(s) need attention, "
                 f"{len(clean)} already covered.\n")
    for finding in flagged:
        lines.append(f"## {finding['alert_name']}  ({finding['count']}x)")
        lines.append(f"- route: `{finding['specialist']}`  case: "
                     f"`{finding['case'] or 'NONE'}`")
        if finding["sample_incidents"]:
            lines.append("- incidents: " +
                         ", ".join(f"#{n}" for n in finding["sample_incidents"]))
        if finding["observed_labels"]:
            lines.append("- labels seen: " + ", ".join(f"`{l}`" for l in finding["observed_labels"]))
        for summary in finding["sample_summaries"][:1]:
            lines.append(f"- summary: {summary}")
        if finding["observed_resolution_minutes"]:
            lines.append("- resolution minutes seen: " +
                         ", ".join(str(m) for m in finding["observed_resolution_minutes"]))
        for issue in finding["issues"]:
            lines.append(f"- **{issue}**")
        lines.append("")

    if clean:
        lines.append("## Already covered")
        for finding in clean:
            lines.append(f"- {finding['alert_name']} ({finding['count']}x) -> "
                         f"`{finding['case']}`")
        lines.append("")

    if comms:
        lines.append("## #comms-noc threads to read")
        lines.append("Compare what the engineer actually did against the case's "
                     "diagnosis_steps/resolution_steps. A changed method is the point of "
                     "this section.\n")
        for thread in sorted(comms, key=lambda t: -t["replies"])[:25]:
            lines.append(f"- #{thread['incident']} ({thread['replies']} replies) "
                         f"{thread['line'][:90]}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--alerts", required=True, type=Path,
                        help="verbatim #alerts-devops dump (connector output or JSON)")
    parser.add_argument("--comms", type=Path,
                        help="verbatim #comms-noc dump, to list threads worth reading")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    result = analyse(load_messages(args.alerts))
    comms = analyse_comms(args.comms.read_text()) if args.comms else []
    if args.json:
        print(json.dumps({"alerts": result, "comms": comms}, indent=2))
    else:
        print(report(result, comms))
    return 0


if __name__ == "__main__":
    sys.exit(main())
