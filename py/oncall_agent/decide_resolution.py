import json
import re
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional

from .slack_history import HistoryFetchResult
from .types import EdgeUiTaskEvidence, VictorOpsIncident

"""
The judgment step. Everything that used to be a hardcoded heuristic
(suggestOwningTeam) or a fixed template (composer.ts) is now decided by an
LLM — shelled out to the `claude` CLI headlessly, grounded in the incident's
real evidence plus real past #comms-noc resolutions of similar incidents
(see slack_history.py), per the 2026-08-24 architecture change.

Hard guardrails (never delegated to the model):
  - The model NEVER sees or invents URLs/permalinks — the only permalink it
    can reference is the one literally supplied in the prompt (or none).
  - The model can only attach evidence by referencing one of the exact
    evidence_keys we tell it exist — it cannot invent a screenshot/log that
    wasn't actually captured.
  - Output is validated against a JSON schema at the CLI layer
    (--json-schema) AND re-validated here before anything is allowed to
    reach Slack; any violation aborts rather than silently sanitizing, since
    a silently "fixed" bad decision is worse than a loud failure here.
"""

ALLOWED_EVIDENCE_KEYS = {"screenshot_tasks", "screenshot_engine", "task_log", "job_log"}

# Descriptions for the four evidence files the Edge UI pipeline always
# produces. Callers that gather MORE evidence (e.g. rendered Grafana panels,
# whose keys are not knowable at import time) pass extra_evidence_keys +
# extra_evidence_descriptions; with neither, everything below behaves exactly
# as it did before that option existed.
_EVIDENCE_DESCRIPTIONS = {
    "screenshot_tasks": "Edge UI Tasks page, filtered to this engine + window",
    "screenshot_engine": "Edge UI Engine page, filtered to this engine + window",
    "task_log": "downloaded task log .zip",
    "job_log": "downloaded job log .zip",
}


def _decision_schema(allowed_keys):
    return {
        "type": "object",
        "properties": {
            "should_post": {"type": "boolean"},
            "reasoning": {
                "type": "string",
                "description": "Internal reasoning for the decision — never posted to Slack, audit-log only.",
            },
            "root_cause_narrative": {
                "type": "string",
                "description": "Plain-language root-cause explanation, or empty string if not yet determinable from the evidence given.",
            },
            "owning_team_mention": {
                "type": "string",
                "description": "e.g. '@engines-team' — must be grounded in the error type/past resolutions, not guessed. Empty string if unclear.",
            },
            "posts": {
                "type": "array",
                "description": "Ordered list of Slack thread replies to post, in order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "evidence_keys": {
                            "type": "array",
                            "items": {"type": "string", "enum": sorted(allowed_keys)},
                        },
                    },
                    "required": ["text", "evidence_keys"],
                },
            },
        },
        "required": ["should_post", "reasoning", "posts", "root_cause_narrative", "owning_team_mention"],
    }


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


def _format_history(history: HistoryFetchResult) -> str:
    if history.unavailable_reason:
        return (
            f"(No historical #comms-noc threads available: {history.unavailable_reason}. "
            "Decide from the current evidence alone — do not invent past incidents.)"
        )
    if not history.threads:
        return "(No matching past #comms-noc threads found.)"
    parts = []
    for i, t in enumerate(history.threads, 1):
        replies = "\n".join(f"    reply: {r}" for r in t.reply_texts[:10])
        parts.append(f"  [{i}] top-level: {t.top_level_text}\n{replies}")
    return "\n".join(parts)


def _build_prompt(
    incident: VictorOpsIncident,
    evidence: EdgeUiTaskEvidence,
    history: HistoryFetchResult,
    available_evidence_keys: List[str],
    tdo_id: Optional[str],
    evidence_descriptions: Optional[Dict[str, str]] = None,
) -> str:
    permalink_note = (
        f'The incident DOES have a real Slack permalink: {incident.slack_permalink} — you may reference '
        f'"Incident #{incident.incident_number}" as linked text if you want, but must not alter the URL.'
        if incident.slack_permalink
        else "The incident has NO Slack permalink available — never invent one; refer to it as plain text only."
    )

    descriptions = {**_EVIDENCE_DESCRIPTIONS, **(evidence_descriptions or {})}
    evidence_menu = "\n".join(
        f"  - {key}: {descriptions.get(key, 'evidence file')}" for key in available_evidence_keys
    )

    return f"""You are deciding how to handle a real VictorOps engine-failure alert for the NOC team, replacing what used to be a hardcoded template. You must replicate the structure and tone of how this NOC team has actually handled engine-failure incidents before (see past resolutions below), while being strictly grounded in the real evidence given — never invent facts, numbers, URLs, or Slack mentions not present here.

INCIDENT
  Number: {incident.incident_number}
  Title: {incident.incident_name}
  {permalink_note}

EVIDENCE (real, gathered from Edge UI — the only facts you may state)
  Engine: {evidence.engine_name} (id {evidence.engine_id})
  Window: last {evidence.window_label}
  Totals: {evidence.total_tasks} tasks, {evidence.completed_tasks} completed ({evidence.completed_pct}%), {evidence.failed_tasks} failed ({evidence.failed_pct}%)
  Org: {evidence.scope_org_name} ({evidence.scope_org_id})
  Error type: {evidence.error_type}
  Raw error lines: {evidence.error_log_lines}
  TDO: {tdo_id or "(not found)"}

EVIDENCE FILES AVAILABLE TO ATTACH (reference ONLY these keys in evidence_keys, never invent others)
  {available_evidence_keys}
{evidence_menu}

PAST #comms-noc RESOLUTIONS OF SIMILAR ENGINE-FAILURE INCIDENTS (ground your structure/tone/team-routing in these; do not copy numbers from them into THIS incident)
{_format_history(history)}

TASK
Decide:
1. should_post — is this evidence strong enough to post (e.g. do we have a real failing engine, a sample error, an org)? If evidence is too thin/ambiguous, set false and explain why in reasoning.
2. root_cause_narrative — a short plain-language explanation of why this failed, based ONLY on the error type/raw error lines above. Empty string if the evidence doesn't support a confident explanation.
3. owning_team_mention — which team (as an "@team" mention) should be notified, based on the error type and how similar past incidents were routed. Empty string if unclear.
4. posts — the ordered list of Slack thread replies (top-level "Alert:" line is handled separately by the caller, do not include it). Each post's text should read like the real NOC engineers writing evidence, not a robotic template dump. Attach evidence_keys where a screenshot/log is relevant to that specific post. The very last post should be the escalation line ending in the owning_team_mention (if any) followed by "FYI^^", matching NOC convention.

Respond with ONLY the structured decision."""


def decide_resolution(
    incident: VictorOpsIncident,
    evidence: EdgeUiTaskEvidence,
    history: HistoryFetchResult,
    available_evidence_keys: List[str],
    tdo_id: Optional[str],
    claude_binary: str = "claude",
    timeout_seconds: int = 120,
    extra_evidence_keys: Optional[List[str]] = None,
    extra_evidence_descriptions: Optional[Dict[str, str]] = None,
) -> Decision:
    allowed_keys = ALLOWED_EVIDENCE_KEYS | set(extra_evidence_keys or [])
    prompt = _build_prompt(
        incident, evidence, history, available_evidence_keys, tdo_id, extra_evidence_descriptions
    )

    result = subprocess.run(
        [
            claude_binary,
            "-p",
            "--output-format",
            "json",
            "--tools",
            "",
            "--json-schema",
            json.dumps(_decision_schema(allowed_keys)),
            prompt,
        ],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI exited {result.returncode}: {result.stderr.strip()}")

    envelope = json.loads(result.stdout)
    if envelope.get("is_error"):
        raise RuntimeError(f"claude CLI reported an error: {envelope}")

    output = envelope.get("structured_output")
    if output is None:
        raise RuntimeError(f"claude CLI did not return structured_output: {result.stdout[:500]}")

    return _validate_and_parse(output, incident, allowed_keys, evidence)


_URL_PATTERN = re.compile(r"https?://[^\s|>)\]]+")


def _normalize_url(url: str) -> str:
    """Trim punctuation prose leaves on a URL, and any trailing slash."""
    return url.rstrip(".,;:!?)]}>|\"'").rstrip("/")


def _urls_in_evidence(incident: VictorOpsIncident,
                      evidence: Optional[EdgeUiTaskEvidence]) -> set:
    """Every URL the model was actually shown, normalized for comparison."""
    corpus = [incident.slack_permalink or "", incident.state_message or ""]
    if evidence is not None:
        corpus.append(" ".join(evidence.error_log_lines or []))
        corpus.append(evidence.error_type or "")
    found = set()
    for text in corpus:
        found.update(_normalize_url(u) for u in _URL_PATTERN.findall(text or ""))
    return found


def _validate_and_parse(
    output: dict, incident: VictorOpsIncident, allowed_keys: Optional[set] = None,
    evidence: Optional[EdgeUiTaskEvidence] = None,
) -> Decision:
    allowed_keys = ALLOWED_EVIDENCE_KEYS if allowed_keys is None else allowed_keys
    posts: List[PlannedPost] = []
    for raw_post in output.get("posts", []):
        keys = raw_post.get("evidence_keys", [])
        bad_keys = [k for k in keys if k not in allowed_keys]
        if bad_keys:
            raise RuntimeError(f"Decision referenced unknown evidence keys {bad_keys} — refusing to post")
        posts.append(PlannedPost(text=raw_post["text"], evidence_keys=keys))

    # Guard against fabricated links. A URL is allowed only if it appears
    # VERBATIM in something we showed the model: the incident's own permalink,
    # or the evidence itself.
    #
    # The evidence part matters and was missing: engine error logs routinely
    # contain URLs — a real run failed on
    # "http://radio.talksport.com/stream" quoted straight out of the failing
    # task's error line. Quoting evidence is the opposite of fabricating, so
    # refusing it blocked a correct thread. What must still be impossible is a
    # URL the model invented, which is why this is exact matching against the
    # supplied text and not a host allow-list.
    allowed_urls = _urls_in_evidence(incident, evidence)
    for post in posts:
        for raw_url in _URL_PATTERN.findall(post.text):
            url = _normalize_url(raw_url)
            if url not in allowed_urls:
                raise RuntimeError(
                    f"Decision text contains a URL that appears nowhere in the incident or its "
                    f"evidence: {raw_url!r} — refusing to post"
                )

    owning_team_mention = output.get("owning_team_mention", "")
    if owning_team_mention and not owning_team_mention.startswith("@"):
        raise RuntimeError(f"owning_team_mention must start with '@' or be empty, got {owning_team_mention!r}")
    if owning_team_mention and not any(owning_team_mention in p.text for p in posts):
        raise RuntimeError(
            f"owning_team_mention {owning_team_mention!r} was decided but doesn't appear in any post text — "
            "the mention is posted only via the posts array, never auto-appended, so this would silently drop the escalation"
        )

    return Decision(
        should_post=bool(output.get("should_post", False)),
        reasoning=output.get("reasoning", ""),
        root_cause_narrative=output.get("root_cause_narrative", ""),
        owning_team_mention=owning_team_mention,
        posts=posts,
    )
