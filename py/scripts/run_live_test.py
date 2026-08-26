#!/usr/bin/env python3
"""
End-to-end live run for the LLM-decided engine-failure pipeline. Same CLI
contract as the previous TypeScript run-live-test.ts, so a different
environment or engine never requires a code edit:

  python run_live_test.py "Incident #119875: [FIRING:1] aiw-prd5001 : Engine failure rate above 15%" \\
      "SI2 Playback segment creator" [windowMinutes] [slackPermalink] [--live]

Dry run by default (POST_MODE): the evidence is gathered for real and the
thread is printed instead of posted. --live posts it.

windowMinutes and slackPermalink are optional and order-independent after the
first two args: a purely numeric trailing arg is the window (default 15,
must be an Edge UI preset), an "http..." trailing arg is the real
VictorOps/Slack permalink (never fabricated — omit it and the top-level line
stays plain, unlinked text).

Evidence gathering, screenshots, and log download are fully deterministic
(they gather facts, not judgment). Root-cause narrative, escalation-team
routing, message structure/content, and whether to post at all are decided
by an LLM (see oncall_agent/Agents/Edgeui_Agent/decide_resolution.py), grounded
in real past #comms-noc resolutions of similar incidents plus this incident's
real evidence — never a hardcoded template or heuristic.

This script is the MANUAL entry point: you name the incident and engine. The
same pipeline runs unattended off real #alerts-devops traffic via
scripts/run_listener.py — both call
oncall_agent.Agents.Edgeui_Agent.engine_failure_pipeline.run_engine_failure_pipeline().
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import bootstrap  # noqa: E402  — must precede third-party imports

bootstrap()

from dotenv import load_dotenv
from slack_sdk import WebClient

from oncall_agent.Agents.Edgeui_Agent.engine_failure_pipeline import run_engine_failure_pipeline
from oncall_agent.Agents.Edgeui_Agent.screenshot import MINUTE_WINDOW_PRESETS
from oncall_agent.types import VictorOpsIncident


def parse_incident_from_args(argv: list) -> tuple:
    if len(argv) < 2:
        raise SystemExit(
            'Usage: python run_live_test.py "<incident line>" "<engine name>" [windowMinutes] [slackPermalink]'
        )
    incident_line, engine_name = argv[0], argv[1]
    rest = argv[2:]
    window_arg = next((a for a in rest if re.fullmatch(r"\d+", a)), None)
    slack_permalink = next((a for a in rest if re.match(r"^https?://", a, re.IGNORECASE)), "")

    number_match = re.search(r"#(\d+)", incident_line)
    incident_number = int(number_match.group(1)) if number_match else 0
    incident_name = re.sub(r"^\s*Incident\s*#\d+:\s*", "", incident_line, flags=re.IGNORECASE).strip()
    window_minutes = int(window_arg) if window_arg else 15
    if window_minutes not in MINUTE_WINDOW_PRESETS:
        raise SystemExit(f"windowMinutes must be one of {MINUTE_WINDOW_PRESETS} (Edge UI's own presets) — got {window_minutes}")

    incident = VictorOpsIncident(
        incident_number=incident_number,
        organization="wazee-digital-inc",
        incident_name=incident_name,
        # Kept as internal matching text only (never rendered) — combining
        # the incident line + engine name here is what lets
        # extract_environment_key and the engine selector find both without
        # any engine/env-specific code.
        entity_display_name=f"{incident_line} {engine_name}",
        monitoring_tool="Alertmanager",
        state_message=f"Engine has failure rate above 15% over the last {window_minutes} minutes.",
        escalation_policy="NOC-VT OnCall",
        slack_permalink=slack_permalink,
        created_at="",
    )
    return incident, window_minutes


def main():
    load_dotenv()
    # --live may appear anywhere; strip it before the positional parse (it is
    # neither numeric nor an http URL, so the scans below would ignore it, but
    # leaving it in would let a typo like "--live" land as the engine name).
    argv = [a for a in sys.argv[1:] if a != "--live"]
    explicit_live = "--live" in sys.argv[1:]
    incident, window_minutes = parse_incident_from_args(argv)

    slack_bot_token = os.environ.get("SLACK_BOT_TOKEN")
    slack_channel = os.environ.get("SLACK_CHANNEL")
    # Honour POST_MODE like every other entry point. This script used to post
    # whenever a token and channel were both present, which became a trap once
    # POST_MODE=dry_run was documented as the safety default: the .env that
    # makes the pipeline work is exactly the .env that made it post. Pass
    # --live (or set POST_MODE=live) for the old behaviour.
    live = explicit_live or (os.environ.get("POST_MODE") or "dry_run").lower() == "live"
    client = WebClient(token=slack_bot_token) if slack_bot_token else None

    if not live:
        print("POST_MODE=dry_run — gathering real evidence, printing the thread "
              "instead of posting. Pass --live (or set POST_MODE=live) to post.\n")

    run_engine_failure_pipeline(
        incident,
        window_minutes=window_minutes,
        client=client,
        channel=slack_channel if (live and slack_bot_token and slack_channel) else None,
        screenshot_dir=Path.cwd() / "dist" / "evidence",
    )

    print("Done.")


if __name__ == "__main__":
    main()
