from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class VictorOpsIncident:
    incident_number: int
    organization: str
    incident_name: str
    entity_display_name: str
    monitoring_tool: str
    state_message: str
    escalation_policy: str
    slack_permalink: str
    created_at: str


@dataclass
class EdgeUiTaskEvidence:
    engine_name: str
    engine_id: str
    window_label: str
    total_tasks: int
    completed_tasks: int
    failed_tasks: int
    completed_pct: int
    failed_pct: int
    scope_org_name: str
    scope_org_id: str
    error_type: str
    error_log_lines: List[str]
    sample_task_id: str
    sample_job_id: str
