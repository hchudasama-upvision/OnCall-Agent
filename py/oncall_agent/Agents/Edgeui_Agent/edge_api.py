from dataclasses import dataclass
from typing import List, Optional

import requests

from .edge_environments import EdgeEnvironment

"""
Real Edge UI/Controller JSON API calls, reverse-engineered from the actual
network traffic of the logged-in Edge UI SPA (captured against
processing.prod1001.aiware.run on 2026-08-21) — not guessed. All three
confirmed to accept the same static Bearer token already used for
/proc/tasks/stats/engines.
"""


@dataclass
class RawTaskRecord:
    internal_task_id: str
    internal_job_id: str
    internal_organization_id: str
    engine_id: str
    engine_name: str
    failure_reason: str
    failure_detail: Optional[str]
    created_date_time: str
    completed_date_time: str
    modified_date_time: str


def _record_from_json(row: dict) -> RawTaskRecord:
    return RawTaskRecord(
        internal_task_id=row.get("internalTaskID", ""),
        internal_job_id=row.get("internalJobID", ""),
        internal_organization_id=row.get("internalOrganizationID", ""),
        engine_id=row.get("engineID", ""),
        engine_name=row.get("engineName", ""),
        failure_reason=row.get("failureReason", ""),
        failure_detail=row.get("failureDetail"),
        created_date_time=row.get("createdDateTime", ""),
        completed_date_time=row.get("completedDateTime", ""),
        modified_date_time=row.get("modifiedDateTime", ""),
    )


def _get_json(url: str, env: EdgeEnvironment, params: Optional[dict] = None) -> dict:
    res = requests.get(
        url,
        params=params,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {env.token}"},
        timeout=20,
    )
    if not res.ok:
        raise RuntimeError(f"Edge UI API call failed ({env.key}): {res.status_code} {res.reason} — {url}")
    return res.json()


def fetch_tasks_by_status(
    env: EdgeEnvironment,
    start_time_epoch_seconds: int,
    end_time_epoch_seconds: int,
    status: str,
    limit: int = 100,
) -> List[RawTaskRecord]:
    body = _get_json(
        f"{env.base_url}/proc/tasks",
        env,
        params={
            "modifiedAfter": start_time_epoch_seconds,
            "modifiedBefore": end_time_epoch_seconds,
            "status": status,
            "limit": limit,
        },
    )
    return [_record_from_json(r) for r in (body.get("result") or [])]


def fetch_task_detail(env: EdgeEnvironment, task_id: str) -> RawTaskRecord:
    body = _get_json(f"{env.base_url}/proc/task/{task_id}/detail", env)
    return _record_from_json(body)


def fetch_organization_name(env: EdgeEnvironment, organization_id: str) -> Optional[str]:
    """Returns None if the org has no `name` populated (observed on some real orgs) — never fabricated."""
    body = _get_json(f"{env.base_url}/admin/organizations", env, params={"organizationID": organization_id})
    result = body.get("result") or []
    return result[0].get("name") if result else None

@dataclass
class EngineBacklog:
    """One engine's backlog over the alert window."""
    engine_id: str
    engine_name: str
    priority: int
    now: int
    peak: int
    points: int
    trend: str          # "climbing" | "draining" | "flat", from the series itself


def fetch_backlog_by_engine(env: EdgeEnvironment, start_time_epoch_seconds: int,
                            end_time_epoch_seconds: int) -> List[EngineBacklog]:
    """Per-engine backlog from the SAME endpoint the Edge UI's own "Backlog"
    card draws: /edge/v1/proc/jobs/backlog_count_by_engine.

    Found by watching the network while loading /processing/jobs/ (2026-08-31).
    Reading the API rather than scraping the chart matters here: the card is an
    ApexCharts SVG whose visible text is only the engine-name legend, so the
    numbers are not in the DOM at all — a scraper would have returned a list of
    engine names and no backlog.

    Response shape: {"counts": [{"engineID", "engineName", "priority",
    "values": [[epoch_ms, count], ...]}], "startDateTime", "endDateTime",
    "success", "error"}.
    """
    payload = _get_json(f"{env.base_url}/proc/jobs/backlog_count_by_engine", env,
                        {"startTime": start_time_epoch_seconds,
                         "endTime": end_time_epoch_seconds})
    if payload.get("error"):
        raise RuntimeError(f"Edge UI backlog call returned an error ({env.key}): "
                           f"{payload['error']}")
    out: List[EngineBacklog] = []
    for entry in payload.get("counts") or []:
        values = [v for _, v in (entry.get("values") or [])
                  if isinstance(v, (int, float))]
        if not values:
            continue
        # Trend from the series' own halves rather than first-vs-last: a single
        # spike at the start would otherwise read as "draining" forever.
        half = max(1, len(values) // 2)
        earlier = sum(values[:half]) / half
        later = sum(values[half:]) / max(1, len(values) - half)
        if later > earlier * 1.25 and later - earlier >= 1:
            trend = "climbing"
        elif earlier > later * 1.25 and earlier - later >= 1:
            trend = "draining"
        else:
            trend = "flat"
        out.append(EngineBacklog(
            engine_id=entry.get("engineID", ""),
            engine_name=entry.get("engineName", "") or entry.get("engineID", "?"),
            priority=int(entry.get("priority") or 0),
            now=int(values[-1]), peak=int(max(values)), points=len(values), trend=trend,
        ))
    out.sort(key=lambda b: (-b.peak, -b.now))
    return out
