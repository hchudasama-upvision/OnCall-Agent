import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from slack_sdk import WebClient

from .decide_resolution import Decision
from .types import VictorOpsIncident

"""
Posts the LLM's decided thread to Slack. The top-level "Alert:" line stays
deterministic (it's a plain fact — the incident number/title/permalink —
not a judgment call), matching the DESIGN.md-documented reference format
(incident #119679). Everything after that is exactly what decide_resolution
produced, already guardrail-checked there.
"""


def compose_top_level_text(incident: VictorOpsIncident) -> str:
    incident_ref = (
        f"<{incident.slack_permalink}|Incident #{incident.incident_number}>"
        if incident.slack_permalink
        else f"Incident #{incident.incident_number}"
    )
    return f"*Alert:*\n> {incident_ref}: {incident.incident_name}"


def post_decided_thread(
    client: WebClient,
    channel: str,
    incident: VictorOpsIncident,
    decision: Decision,
    evidence_file_paths: Dict[str, Path],
    log: Callable[[str], None] = print,
) -> None:
    if not decision.should_post:
        log(f"Not posting — decision was should_post=False. Reasoning: {decision.reasoning}")
        return

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
        self.log(f"\n(owning_team_mention metadata: {decision.owning_team_mention or '(none)'})")
