import re
import time
from typing import List, Optional

from .edge_api import fetch_organization_name, fetch_task_detail, fetch_tasks_by_status
from .edge_environments import EdgeEnvironment, extract_environment_key
from .engine_task_stats import EngineTaskCounts, active_task_total, fetch_engine_task_stats
from .types import EdgeUiTaskEvidence, VictorOpsIncident

"""
Real Edge UI-backed evidence gathering. Endpoints confirmed by capturing the
logged-in SPA's own network traffic (2026-08-21) — /proc/tasks/stats/engines,
/proc/tasks (status-filtered list), /proc/task/{id}/detail, and
/admin/organizations all accept the same static Bearer token already
verified for stats/engines.

Deliberately gathers RAW evidence only. Root-cause narrative and escalation
routing are no longer decided here — those are judgment calls now made by
the LLM decision step (see decide_resolution.py), grounded in past
#comms-noc resolutions, per the 2026-08-24 architecture change.
"""


def _parse_window_minutes(state_message: str) -> Optional[int]:
    match = re.search(r"last\s+(\d+)\s*(hour|minute)s?", state_message, re.IGNORECASE)
    if not match:
        return None
    amount = int(match.group(1))
    return amount * 60 if match.group(2).lower() == "hour" else amount


def format_window_label(minutes: int) -> str:
    if minutes % 60 == 0:
        hours = minutes // 60
        return "1 hour" if hours == 1 else f"{hours} hours"
    return "1 minute" if minutes == 1 else f"{minutes} minutes"


def _select_failing_engine(
    engines: List[EngineTaskCounts], incident: VictorOpsIncident
) -> Optional[EngineTaskCounts]:
    """Picks the engine the incident text names, if any; otherwise the one with the most failures."""
    haystack = f"{incident.incident_name} {incident.entity_display_name} {incident.state_message}".lower()
    named = next((e for e in engines if e.engine_name and e.engine_name.lower() in haystack), None)
    if named and named.counts.get("failed", 0) > 0:
        return named

    failing = [e for e in engines if e.counts.get("failed", 0) > 0]
    failing.sort(key=lambda e: e.counts.get("failed", 0), reverse=True)
    return failing[0] if failing else None


def get_task_evidence(
    incident: VictorOpsIncident, environments: dict, now_epoch_seconds: Optional[int] = None
) -> EdgeUiTaskEvidence:
    env_key = extract_environment_key(f"{incident.incident_name} {incident.entity_display_name}")
    if not env_key:
        raise RuntimeError(
            f'Could not find an "aiw-xxx" environment key in incident #{incident.incident_number}\'s title'
        )
    env: Optional[EdgeEnvironment] = environments.get(env_key)
    if not env:
        raise RuntimeError(
            f'No confirmed Edge UI environment configured for "{env_key}" (incident #{incident.incident_number})'
        )

    window_minutes = _parse_window_minutes(incident.state_message) or 15
    end_time = now_epoch_seconds if now_epoch_seconds is not None else int(time.time())
    start_time = end_time - window_minutes * 60

    engine_stats = fetch_engine_task_stats(env, start_time, end_time)
    engine = _select_failing_engine(engine_stats, incident)
    if not engine:
        raise RuntimeError(f"No engine with failed tasks found in {env_key} over the last {window_minutes} minutes")

    total_tasks = active_task_total(engine)
    failed_tasks = engine.counts.get("failed", 0)
    completed_tasks = engine.counts.get("complete", 0)
    failed_pct = round((failed_tasks / total_tasks) * 100) if total_tasks > 0 else 0
    completed_pct = round((completed_tasks / total_tasks) * 100) if total_tasks > 0 else 0

    failed_records = fetch_tasks_by_status(env, start_time, end_time, status="failed", limit=100)
    matching = [r for r in failed_records if r.engine_id == engine.engine_id]
    matching.sort(key=lambda r: r.modified_date_time, reverse=True)
    sample = matching[0] if matching else None
    if not sample:
        raise RuntimeError(
            f"Engine {engine.engine_name} shows {failed_tasks} failed in stats but no matching task record was found"
        )

    detail = fetch_task_detail(env, sample.internal_task_id)
    org_name = fetch_organization_name(env, detail.internal_organization_id)

    return EdgeUiTaskEvidence(
        engine_name=engine.engine_name,
        engine_id=engine.engine_id,
        window_label=format_window_label(window_minutes),
        total_tasks=total_tasks,
        completed_tasks=completed_tasks,
        failed_tasks=failed_tasks,
        completed_pct=completed_pct,
        failed_pct=failed_pct,
        scope_org_name=org_name or f"org {detail.internal_organization_id}",
        scope_org_id=detail.internal_organization_id,
        error_type=detail.failure_reason,
        error_log_lines=[detail.failure_detail] if detail.failure_detail else [],
        sample_task_id=detail.internal_task_id,
        sample_job_id=detail.internal_job_id,
    )
