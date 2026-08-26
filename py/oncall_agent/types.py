from dataclasses import dataclass, field
from typing import Dict, List, Optional


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
class PlannedPost:
    text: str
    evidence_keys: List[str]


@dataclass
class Decision:
    should_post: bool
    reasoning: str
    root_cause_narrative: str
    owning_team_mention: str
    posts: List[PlannedPost]
    # A state-changing step the agent believes is needed but must NOT perform.
    # {"summary", "command", "risk"} or None. Posted for a human to approve;
    # nothing in this repo can execute it.
    proposed_action: Optional[dict] = None


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


@dataclass
class ParsedAlert:
    """One message from #alerts-devops, normalized.

    Source classification follows DESIGN.md §1.1 (five posting sources) and
    the v1.1 scope refinement: only VictorOps *incidents* are triggers. Raw
    Alertmanager / PandoLogic / Jenkins posts are kept as correlation
    context and never start an investigation on their own — that is what
    `is_trigger` encodes.
    """

    source: str                     # victorops | alertmanager | pandologic | jenkins | oncall_rotation | unknown
    kind: str                       # incident | incident_update | warning | report | rotation | unknown
    raw_text: str
    channel_id: str = ""
    message_ts: str = ""
    permalink: str = ""
    incident_number: int = 0
    incident_name: str = ""
    entity_display_name: str = ""
    # The VictorOps portal URL out of the card's own heading. This — not a
    # Slack permalink — is the link a human puts in the #comms-noc "Alert:"
    # line, so it is what the agent posts too.
    incident_url: str = ""
    alert_phase: str = ""           # CURRENT_ALERT_PHASE: FIRING|ACKED|RESOLVED
    alert_state: str = ""           # CURRENT_STATE: CRITICAL|WARNING|OK
    acked_by: str = ""
    monitoring_tool: str = ""
    state_message: str = ""
    escalation_policy: str = ""
    alert_name: str = ""            # the Alertmanager alertname, e.g. "KubePodCrashLooping"
    environment_key: str = ""       # the "aiw-xxx" token, when the alert carries one
    firing_count: int = 0
    # When the alert is ALREADY visible in the comms channel — a human posted
    # the "Alert:" line, or a replay posted the card — the agent replies into
    # that thread instead of opening a second top-level message for the same
    # incident. Empty means "open a new thread".
    reply_in_thread_ts: str = ""
    # Alertmanager label VALUES from the alert's parentheses, plus a
    # shape-based guess at what they mean. These are what fill a Grafana
    # template variable — which host, which VM, which volume.
    # Who posted it — bot_id for integrations, user id for humans. Used to
    # refuse our own messages when reading and writing one channel.
    author_id: str = ""
    labels: List[str] = field(default_factory=list)
    label_hints: Dict[str, str] = field(default_factory=dict)
    fields: Dict[str, str] = field(default_factory=dict)

    @property
    def is_resolved(self) -> bool:
        """Already closed before the agent got to it.

        Extremely common: engine-failure incidents on prd5001 page roughly
        every two hours and auto-resolve with RESOLVED_BY: SYSTEM. Opening an
        investigation thread on one of those adds noise to #comms-noc without
        adding information.
        """
        return self.alert_phase.upper() == "RESOLVED" or self.alert_state.upper() == "OK"

    @property
    def is_trigger(self) -> bool:
        return self.source == "victorops" and self.kind == "incident" and not self.is_resolved

    @property
    def fingerprint(self) -> str:
        """(alert name, env, entity) collapsed to one string, for dedup/suppression."""
        parts = [p for p in (self.alert_name or self.incident_name, self.environment_key,
                             self.entity_display_name) if p]
        return "|".join(parts).lower()
