#!/usr/bin/env python3
"""
Handles any VictorOps alert with no built SOP (no evidence-gathering
pipeline, no fixed template — see engine-failure's run_live_test.py for
that path). Investigates real #comms-noc/#alerts-devops history for this
SAME alert type and mirrors it, per 2026-08-24 direction — never fabricates
facts about the current occurrence beyond its own real title/number.

Usage:
  python scripts/run_generic_alert.py "Incident #119890: Site DNS Valdation"
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
from dotenv import load_dotenv
from slack_sdk import WebClient

from oncall_agent.generic_alert_handler import investigate_and_compose
from oncall_agent.recurrence_store import lookup_existing_thread, record_thread
from oncall_agent.slack_post import DryRunSlackPoster, post_composed_thread
from oncall_agent.types import VictorOpsIncident


def parse_incident_line(line: str) -> VictorOpsIncident:
    number_match = re.search(r"#(\d+)", line)
    incident_number = int(number_match.group(1)) if number_match else 0
    incident_name = re.sub(r"^\s*Incident\s*#\d+:\s*", "", line, flags=re.IGNORECASE).strip()
    return VictorOpsIncident(
        incident_number=incident_number,
        organization="wazee-digital-inc",
        incident_name=incident_name,
        entity_display_name=line,
        monitoring_tool="Alertmanager",
        state_message="",
        escalation_policy="NOC-VT OnCall",
        slack_permalink="",
        created_at="",
    )


def main():
    load_dotenv()
    if len(sys.argv) < 2:
        raise SystemExit('Usage: python scripts/run_generic_alert.py "<incident line>"')

    incident = parse_incident_line(sys.argv[1])
    # Key by incident number: a second run against the SAME incident (e.g.
    # posting a follow-up once new evidence comes in) threads onto the post
    # this pipeline already made, instead of starting a new one every time —
    # same mechanism as engine-failure's recurrence_store, just keyed
    # per-incident here since generic alerts don't have a stable
    # engine/error fingerprint to group by.
    fingerprint = f"incident:{incident.incident_number}"
    existing_thread = lookup_existing_thread(fingerprint)
    thread = investigate_and_compose(incident, existing_thread=existing_thread)

    slack_bot_token = os.environ.get("SLACK_BOT_TOKEN")
    slack_channel = os.environ.get("SLACK_CHANNEL")
    if slack_bot_token and slack_channel:
        client = WebClient(token=slack_bot_token)
        posted_channel, posted_thread_ts = post_composed_thread(client, slack_channel, thread, {})
        if posted_thread_ts:
            record_thread(fingerprint, posted_channel, posted_thread_ts, incident.incident_number)
    else:
        DryRunSlackPoster().post_composed_thread(thread, {})

    print("Done.")


if __name__ == "__main__":
    main()
