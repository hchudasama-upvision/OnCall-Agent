from dataclasses import dataclass
from typing import List, Optional, Tuple

from .decide_resolution import Judgment
from .types import EdgeUiTaskEvidence, VictorOpsIncident

"""
Fixed message template matching the NOC's own established #comms-noc
convention (confirmed against a real reference post 2026-08-24) — ordering,
field labels, and code-formatting of engine/org/error/TDO badges. Only the
root-cause narrative and escalation mention (from decide_resolution.py's
Judgment) are LLM-authored; everything else here is filled from real
evidence, never generated.

Recurrence threading: matching real NOC practice (confirmed against a real
reference thread 2026-08-24, in the same workspace this pipeline posts to)
— recurring incidents keep the SAME "Alert:" label every time, appended as
broadcast replies (reply_broadcast, i.e. Slack's "also send to channel") in
the SAME long-running thread, rather than switching wording or starting a
new thread per occurrence. The thread to append to is looked up from this
pipeline's OWN prior posts (see recurrence_store.py) — never the real
#comms-noc thread the model found during grounding, since that thread lives
in a different workspace this pipeline doesn't post into.
"""

ALLOWED_EVIDENCE_KEYS = {"screenshot_tasks", "screenshot_engine", "task_log", "job_log"}


@dataclass
class PlannedPost:
    text: str
    evidence_keys: List[str]


@dataclass
class ComposedThread:
    top_level_text: str
    should_post: bool
    posts: List[PlannedPost]
    # (channel_id, thread_ts) of a real existing #comms-noc thread to append
    # to, when this is a known recurring issue — None means post as a fresh
    # top-level message in the configured channel instead.
    existing_thread: Optional[Tuple[str, str]] = None


def compose_top_level_text(incident: VictorOpsIncident) -> str:
    incident_ref = (
        f"<{incident.slack_permalink}|Incident #{incident.incident_number}>"
        if incident.slack_permalink
        else f"Incident #{incident.incident_number}"
    )
    return f"*Alert:*\n> {incident_ref}: {incident.incident_name}"


def compose_thread(
    incident: VictorOpsIncident,
    evidence: EdgeUiTaskEvidence,
    tdo_id: Optional[str],
    judgment: Judgment,
    local_existing_thread: Optional[Tuple[str, str]] = None,
) -> ComposedThread:
    # Exact known repeat with an established thread to ping: a bare alert
    # line is enough (matches real NOC practice — no re-explaining or
    # re-tagging what's already documented above in the thread). Only holds
    # when we actually HAVE a prior thread to reply into; if this is the
    # first time we're posting about it in this channel, there's nothing
    # "already documented" here yet, so fall through to a full post
    # regardless of what the model found in the (different) real history.
    if judgment.is_known_recurring_issue and judgment.is_exact_known_repeat and local_existing_thread:
        return ComposedThread(
            top_level_text=compose_top_level_text(incident),
            should_post=judgment.should_post,
            posts=[],
            existing_thread=local_existing_thread,
        )

    impact_post = PlannedPost(
        text="\n".join(
            [
                f"Engine: `{evidence.engine_name}`",
                f"Engine ID: `{evidence.engine_id}`",
                "",
                f"In the last {evidence.window_label}, {evidence.failed_tasks} ({evidence.failed_pct}%) tasks failed.",
            ]
        ),
        evidence_keys=["screenshot_tasks"],
    )

    scope_post = PlannedPost(
        text="\n".join(
            [
                f"Traffic from: {evidence.scope_org_name} (`{evidence.scope_org_id}`)",
                f"Error Type: `{evidence.error_type}`",
            ]
        ),
        evidence_keys=["screenshot_engine"],
    )

    raw_error_post = PlannedPost(
        text="```" + "\n".join(evidence.error_log_lines) + "```",
        evidence_keys=[],
    )

    logs_post = PlannedPost(
        text=f"TDO: `{tdo_id}`" if tdo_id else "TDO: (not found)",
        evidence_keys=["task_log", "job_log"],
    )

    posts = [impact_post, scope_post, raw_error_post, logs_post]

    # The escalation @mention always comes last, after all evidence —
    # matching NOC convention. Omit the reply entirely if there's neither a
    # narrative nor a mention to say (rather than posting an empty message).
    closing_lines = []
    if judgment.root_cause_narrative:
        closing_lines.append(judgment.root_cause_narrative)
        closing_lines.append("")
    if judgment.owning_team_mention:
        closing_lines.append(f"{judgment.owning_team_mention} FYI^^")
    if closing_lines:
        posts.append(PlannedPost(text="\n".join(closing_lines), evidence_keys=[]))

    return ComposedThread(
        top_level_text=compose_top_level_text(incident),
        should_post=judgment.should_post,
        posts=posts,
        existing_thread=local_existing_thread,
    )
