import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from slack_sdk import WebClient

from .types import Decision, VictorOpsIncident

"""
Posts the LLM's decided thread to Slack. The top-level "Alert:" line stays
deterministic (it's a plain fact — the incident number/title/permalink —
not a judgment call), matching the DESIGN.md-documented reference format
(incident #119679). Everything after that is exactly what decide_resolution
produced, already guardrail-checked there.
"""


def compose_top_level_text(incident: VictorOpsIncident) -> str:
    """The "Alert:" header, byte-matching what the on-call engineers post.

    Verified against real #comms-noc messages (2026-08-24, incidents #119884
    and #119869): the word "Alert:" is NOT bold, the quoted line IS bold in
    full, and the link is the VictorOps PORTAL url — not a Slack permalink.
    The earlier shape (`*Alert:*` + an unbolded quote + a Slack permalink) was
    written from the design doc before the channel had been read, and did not
    match any message actually in it.
    """
    incident_ref = (
        f"<{incident.slack_permalink}|Incident #{incident.incident_number}>"
        if incident.slack_permalink
        else f"Incident #{incident.incident_number}"
    )
    return f"Alert:\n> *{incident_ref}: {incident.incident_name}*"


# Owner's direction, 2026-08-27: the thread carries OBSERVATIONS ONLY — no
# recommendation lines and no "Proposed action" block. Every agent's prompt says
# to leave proposed_action empty, but a prompt is a request and this is the lock:
# code decides what gets posted (CLAUDE.md non-negotiable #1), so a model that
# fills the field anyway still cannot put an action request in the channel.
# compose_action_request/post_action_request below are deliberately kept — the
# approve/deny plumbing is the rollback path if the owner wants it back; flipping
# this one flag is the whole change.
POST_PROPOSED_ACTION = False

def compose_action_request(action: dict) -> str:
    """The approval ask, worded so nobody can read it as work already done."""
    lines = [":raised_hand: *Proposed action — NOT performed. Needs a human.*",
             f"> {action['summary']}"]
    if action.get("command"):
        lines.append(f"```{action['command']}```")
    if action.get("risk"):
        lines.append(f"_Risk:_ {action['risk']}")
    lines.append("_The agent is read-only and cannot run this. Someone has to._")
    return "\n".join(lines)


def post_action_request(client, channel: str, thread_ts: str, action: dict,
                        with_buttons: bool = False,
                        log: Callable[[str], None] = print) -> None:
    """Post a state-changing step for a human to approve and run.

    The buttons record a decision and nothing else — there is no executor, by
    design (the owner's rule, 2026-08-24: the agent may read and report, and
    must ask before anything is changed). They exist so the ask has an
    auditable answer, not so it can be actioned by clicking.
    """
    text = compose_action_request(action)
    if not with_buttons:
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
        log("posted proposed action (no buttons — Socket Mode not enabled)")
        return
    client.chat_postMessage(
        channel=channel, thread_ts=thread_ts, text=text,
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {"type": "actions", "block_id": "remediation", "elements": [
                {"type": "button", "action_id": "approve_remediation", "style": "primary",
                 "value": action["summary"][:2000],
                 "text": {"type": "plain_text", "text": "Approve"}},
                {"type": "button", "action_id": "deny_remediation", "style": "danger",
                 "value": action["summary"][:2000],
                 "text": {"type": "plain_text", "text": "Deny"}}]},
            {"type": "context", "elements": [{"type": "mrkdwn",
             "text": "_Records the decision only — there is no executor. "
                     "Whoever approves still runs it._"}]},
        ])
    log("posted proposed action with approve/deny")


def post_decided_thread(
    client: WebClient,
    channel: str,
    incident: VictorOpsIncident,
    decision: Decision,
    evidence_file_paths: Dict[str, Path],
    log: Callable[[str], None] = print,
    thread_ts: str = "",
    with_buttons: bool = False,
) -> None:
    """Post the decided thread. With thread_ts, reply into an existing one.

    thread_ts matters because the alert is often already in the channel — a
    human posted the "Alert:" line, or a replay posted the card. Opening a
    second top-level message for the same incident splits the conversation,
    which is the opposite of what recurrences do today (they append to the
    original thread).
    """
    if not decision.should_post:
        log(f"Not posting — decision was should_post=False. Reasoning: {decision.reasoning}")
        return

    if thread_ts:
        log(f"replying into existing thread {thread_ts}")
    else:
        top = client.chat_postMessage(channel=channel, text=compose_top_level_text(incident))
        thread_ts = top["ts"]

    for post in decision.posts:
        file_paths = [evidence_file_paths[k] for k in post.evidence_keys if k in evidence_file_paths]
        if file_paths:
            client.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                initial_comment=post.text,
                file_uploads=[{"file": str(p), "filename": p.name} for p in file_paths],
            )
            # files_upload_v2 resolving doesn't mean the file has finished
            # rendering in the channel yet — a plain text reply posted right
            # after can visibly land before it. Give it a moment to catch up
            # (same fix as the TS client's WebApiSlackClient).
            time.sleep(2)
        else:
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=post.text)

    if decision.proposed_action and (decision.proposed_action.get("summary") or "").strip():
        if POST_PROPOSED_ACTION:
            post_action_request(client, channel, thread_ts, decision.proposed_action,
                                with_buttons=with_buttons, log=log)
        else:
            log("proposed_action suppressed (observations-only thread): "
                f"{str(decision.proposed_action.get('summary'))[:120]}")


class DryRunSlackPoster:
    """Prints what would be posted instead of calling the Slack API — default until explicitly wired to a real token+channel."""

    def __init__(self, log: Callable[[str], None] = print):
        self.log = log

    def post_decided_thread(
        self,
        incident: VictorOpsIncident,
        decision: Decision,
        evidence_file_paths: Dict[str, Path],
    ) -> None:
        if not decision.should_post:
            self.log(f"[DRY RUN] would NOT post — should_post=False. Reasoning: {decision.reasoning}")
            return
        self.log(f"\n[DRY RUN] would post top-level:\n{compose_top_level_text(incident)}")
        for post in decision.posts:
            files = [str(evidence_file_paths[k]) for k in post.evidence_keys if k in evidence_file_paths]
            self.log(f"\n[DRY RUN] would reply:\n{post.text}" + (f"\n  files: {files}" if files else ""))
        summary = (decision.proposed_action or {}).get("summary") or ""
        if summary.strip():
            if POST_PROPOSED_ACTION:
                self.log(f"\n[DRY RUN] would ask for approval:\n"
                         f"{compose_action_request(decision.proposed_action)}")
            else:
                self.log(f"\n[DRY RUN] proposed_action suppressed "
                         f"(observations-only thread): {summary[:120]}")
        self.log(f"\n(owning_team_mention metadata: {decision.owning_team_mention or '(none)'})")
