import os
from pathlib import Path
from typing import Callable, Dict, List, Optional

from slack_sdk import WebClient

from . import evidence_panels, grafana
from .decide_resolution import Decision, decide_resolution
from .edge_environments import extract_environment_key, load_edge_environments, to_ui_base_url
from .live_edge_ui_client import get_task_evidence
from .screenshot import (
    MINUTE_WINDOW_PRESETS,
    capture_filtered_edge_ui_view,
    download_task_and_job_logs,
    with_edge_ui_session,
)
from .slack_history import HistoryFetchResult, fetch_relevant_comms_noc_history
from .slack_post import DryRunSlackPoster, post_decided_thread
from .types import VictorOpsIncident

"""
The engine-failure-rate pipeline, lifted verbatim out of
scripts/run_live_test.py so the listener and the manual CLI run the SAME
code path. Behavior is unchanged from that script — same evidence, same
window sanity checks, same abort conditions, same Slack output — with one
addition: if config/panel_map.json maps this alert type to Grafana panels,
those PNGs are rendered and offered to the decision step as extra
attachable evidence. With no mapping (the shipped default) the run is
identical to before.

Evidence gathering, screenshots and log download stay fully deterministic —
they gather facts, not judgment. Root-cause narrative, escalation-team
routing, message structure, and whether to post at all are decided by the
LLM in decide_resolution.py, grounded in real past #comms-noc resolutions.
"""


def run_engine_failure_pipeline(
    incident: VictorOpsIncident,
    window_minutes: int = 15,
    client: Optional[WebClient] = None,
    channel: Optional[str] = None,
    screenshot_dir: Optional[Path] = None,
    history: Optional[HistoryFetchResult] = None,
    panel_fingerprint: str = "",
    log: Callable[[str], None] = print,
) -> Decision:
    """Gather Edge UI evidence for one engine-failure incident, decide, post.

    client/channel unset (or either missing) => dry run: everything is
    gathered for real and the Slack output is printed instead of sent.
    Returns the Decision so callers can audit-log it.
    """
    if window_minutes not in MINUTE_WINDOW_PRESETS:
        raise SystemExit(
            f"windowMinutes must be one of {MINUTE_WINDOW_PRESETS} (Edge UI's own presets) "
            f"— got {window_minutes}"
        )

    screenshot_dir = screenshot_dir or (Path.cwd() / "dist" / "evidence")
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

    evidence_file_paths: Dict[str, Path] = {
        "screenshot_tasks": tasks_page_path,
        "screenshot_engine": engine_page_path,
        "task_log": logs.task_log_path,
        "job_log": logs.job_log_path,
    }

    # Optional Grafana panels, keyed off the same panel map the triage path
    # uses. Only ever additive: no mapping (the shipped default) or no
    # Grafana credentials means the decision sees exactly the four Edge UI
    # evidence files it always has.
    extra_keys, extra_descriptions = _attach_grafana_panels(
        evidence_file_paths, panel_fingerprint or incident.incident_name,
        env_key, screenshot_dir, log,
    )

    if history is None:
        history = (
            fetch_relevant_comms_noc_history(client, ["engine failure"])
            if client
            else HistoryFetchResult(threads=[], unavailable_reason="no Slack client configured (dry run)")
        )

    decision = decide_resolution(
        incident, evidence, history, list(evidence_file_paths.keys()), logs.tdo_id,
        extra_evidence_keys=extra_keys,
        extra_evidence_descriptions=extra_descriptions,
    )

    if client and channel:
        post_decided_thread(client, channel, incident, decision, evidence_file_paths)
    else:
        DryRunSlackPoster(log=log).post_decided_thread(incident, decision, evidence_file_paths)

    return decision


def _attach_grafana_panels(
    evidence_file_paths: Dict[str, Path],
    fingerprint: str,
    env_key: Optional[str],
    out_dir: Path,
    log: Callable[[str], None],
) -> tuple:
    """Render mapped Grafana panels into evidence_file_paths. Never fatal.

    Grafana being down, unmapped, or unreachable over VPN must not cost us
    the Edge UI evidence we already have — the whole point of this pipeline
    is that it posts the facts it managed to gather.
    """
    if not grafana.is_configured():
        return [], {}
    try:
        specs = evidence_panels.load_panel_map().specs_for(fingerprint, env_key)
        rendered = evidence_panels.render_panels(specs, out_dir, log=log) if specs else []
    except Exception as e:                          # noqa: BLE001 — operational, not a bug
        log(f"Grafana evidence skipped: {str(e).splitlines()[0]}")
        return [], {}

    extra_keys: List[str] = []
    extra_descriptions: Dict[str, str] = {}
    for panel in rendered:
        key = f"grafana_{panel.spec.dashboard_uid}_{panel.spec.panel_id}".replace("-", "_")
        evidence_file_paths[key] = panel.path
        extra_keys.append(key)
        extra_descriptions[key] = evidence_panels.panel_caption(panel.spec)
    return extra_keys, extra_descriptions
