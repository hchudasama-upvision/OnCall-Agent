#!/usr/bin/env python3
"""
End-to-end live run for the LLM-decided engine-failure pipeline. Same CLI
contract as the previous TypeScript run-live-test.ts, so a different
environment or engine never requires a code edit:

  python run_live_test.py "Incident #119875: [FIRING:1] aiw-prd5001 : Engine failure rate above 15%" \\
      "SI2 Playback segment creator" [windowMinutes] [slackPermalink]

windowMinutes and slackPermalink are optional and order-independent after the
first two args: a purely numeric trailing arg is the window (default 15,
must be an Edge UI preset), an "http..." trailing arg is the real
VictorOps/Slack permalink (never fabricated — omit it and the top-level line
stays plain, unlinked text).

What changed vs. the TS version (2026-08-24 architecture pivot): evidence
gathering, screenshots, and log download are still fully deterministic (they
gather facts, not judgment). But root-cause narrative, escalation-team
routing, message structure/content, and whether to post at all are now
decided by an LLM (see oncall_agent/decide_resolution.py), grounded in real
past #comms-noc resolutions of similar incidents plus this incident's real
evidence — never a hardcoded template or heuristic.
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from slack_sdk import WebClient

from oncall_agent.decide_resolution import decide_resolution
from oncall_agent.edge_environments import extract_environment_key, load_edge_environments, to_ui_base_url
from oncall_agent.screenshot import MINUTE_WINDOW_PRESETS, download_task_and_job_logs, capture_filtered_edge_ui_view, with_edge_ui_session
from oncall_agent.slack_history import HistoryFetchResult, fetch_relevant_comms_noc_history
from oncall_agent.slack_post import DryRunSlackPoster, post_decided_thread
from oncall_agent.live_edge_ui_client import get_task_evidence
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
    incident, window_minutes = parse_incident_from_args(sys.argv[1:])

    screenshot_dir = Path.cwd() / "dist" / "evidence"
    screenshot_dir.mkdir(parents=True, exist_ok=True)

    environments = load_edge_environments()
    evidence = get_task_evidence(incident, environments)

    env_key = extract_environment_key(f"{incident.incident_name} {incident.entity_display_name}")
    env = environments.get(env_key) if env_key else None
    if not env:
        raise SystemExit(f"No environment configured for {env_key}")

    edge_username = os.environ.get("EDGE_USERNAME")
    edge_password = os.environ.get("EDGE_PASSWORD")
    if not edge_username or not edge_password:
        raise SystemExit("EDGE_USERNAME/EDGE_PASSWORD not set — required for real Edge UI screenshots")

    tasks_page_path = screenshot_dir / "live-test-tasks-page.png"
    engine_page_path = screenshot_dir / "live-test-engine-page.png"
    ui_base_url = to_ui_base_url(env)

    with with_edge_ui_session(ui_base_url, edge_username, edge_password) as page:
        tasks_result = capture_filtered_edge_ui_view(
            page, f"{ui_base_url}/processing/tasks/", evidence.engine_name, window_minutes, tasks_page_path
        )
        engine_result = capture_filtered_edge_ui_view(
            page, f"{ui_base_url}/processing/engine/", evidence.engine_name, window_minutes, engine_page_path
        )
        logs = download_task_and_job_logs(
            page, ui_base_url, evidence.sample_task_id, evidence.sample_job_id, screenshot_dir
        )

    if not tasks_result.stats:
        raise SystemExit("Could not scrape completed/failed stats off the Tasks page")

    # Sanity check: both screenshots should show the same window we set.
    # This must abort, not just warn — posting text that says "15 minutes"
    # next to a screenshot actually showing a different window is exactly
    # the drift bug this pipeline exists to prevent.
    expected_prefix = str(window_minutes)
    if not tasks_result.actual_window_label.startswith(expected_prefix):
        raise SystemExit(f'Aborting: Tasks page window unexpectedly "{tasks_result.actual_window_label}", not {window_minutes} minutes')
    if not engine_result.actual_window_label.startswith(expected_prefix):
        raise SystemExit(f'Aborting: Engine page window unexpectedly "{engine_result.actual_window_label}", not {window_minutes} minutes')

    # Use the numbers scraped straight off the Tasks page screenshot, not a
    # separately-timed stats-API call — the two are fetched several seconds
    # apart and real task counts drift that fast.
    evidence.completed_tasks = tasks_result.stats.completed_tasks
    evidence.completed_pct = tasks_result.stats.completed_pct
    evidence.failed_tasks = tasks_result.stats.failed_tasks
    evidence.failed_pct = tasks_result.stats.failed_pct

    evidence_file_paths = {
        "screenshot_tasks": tasks_page_path,
        "screenshot_engine": engine_page_path,
        "task_log": logs.task_log_path,
        "job_log": logs.job_log_path,
    }

    slack_bot_token = os.environ.get("SLACK_BOT_TOKEN")
    slack_channel = os.environ.get("SLACK_CHANNEL")
    client = WebClient(token=slack_bot_token) if slack_bot_token else None

    history = (
        fetch_relevant_comms_noc_history(client, ["engine failure"])
        if client
        else HistoryFetchResult(threads=[], unavailable_reason="no Slack client configured (dry run)")
    )

    decision = decide_resolution(
        incident, evidence, history, list(evidence_file_paths.keys()), logs.tdo_id
    )

    if slack_bot_token and slack_channel:
        post_decided_thread(client, slack_channel, incident, decision, evidence_file_paths)
    else:
        DryRunSlackPoster().post_decided_thread(incident, decision, evidence_file_paths)

    print("Done.")


if __name__ == "__main__":
    main()
