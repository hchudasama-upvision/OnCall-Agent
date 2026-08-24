from dataclasses import dataclass, field
from typing import Dict, List

import requests

from .edge_environments import EdgeEnvironment

"""
Real Edge UI/Controller query, mirroring `engine_task_stats.sh`
(DevOps repo, cron on ops-monitoring1-ops): GET /proc/tasks/stats/engines
for a window, aggregated per engine per task status.
"""

_NON_ACTIVE_STATUSES = {"aborted", "scheduled", "rejected"}


@dataclass
class EngineTaskCounts:
    engine_id: str
    engine_name: str
    counts: Dict[str, int] = field(default_factory=dict)


def fetch_engine_task_stats(
    env: EdgeEnvironment, start_time_epoch_seconds: int, end_time_epoch_seconds: int
) -> List[EngineTaskCounts]:
    url = f"{env.base_url}/proc/tasks/stats/engines"
    res = requests.get(
        url,
        params={"startTime": start_time_epoch_seconds, "endTime": end_time_epoch_seconds},
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {env.token}"},
        timeout=20,
    )
    if not res.ok:
        raise RuntimeError(f"Edge UI stats query failed for {env.key}: {res.status_code} {res.reason}")

    body = res.json()
    by_engine: Dict[str, EngineTaskCounts] = {}
    for row in body.get("counts") or []:
        engine_id = row.get("engineID")
        status = row.get("status")
        if not engine_id or not status:
            continue
        existing = by_engine.setdefault(
            engine_id, EngineTaskCounts(engine_id=engine_id, engine_name=row.get("engineName", ""))
        )
        existing.counts[status] = existing.counts.get(status, 0) + (row.get("count") or 0)
    return list(by_engine.values())


def active_task_total(engine: EngineTaskCounts) -> int:
    """Total active (non-aborted/scheduled/rejected) task count for an engine, per the script's tiering logic."""
    return sum(count for status, count in engine.counts.items() if status not in _NON_ACTIVE_STATUSES)
