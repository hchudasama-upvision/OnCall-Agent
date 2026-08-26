import json
import re
import subprocess
from typing import Dict, List, Optional

from ...slack_history import HistoryFetchResult
from ...types import Decision, EdgeUiTaskEvidence, PlannedPost, VictorOpsIncident

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


def _format_past_cases(past_cases: Optional[List[dict]]) -> str:
    if not past_cases:
        return "(no case-library entry for this engine-failure fingerprint)"
    return json.dumps(past_cases, indent=2)


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
    past_cases: Optional[List[dict]] = None,
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

KNOWN ROOT CAUSES FOR THIS ALERT TYPE (from a prior real-history analysis of this engine's/alert type's past incidents — a HEAD START, not a fact about THIS occurrence. Each entry lists the error_code it was seen with, a source_permalink, and a fix_reference that may contain a real confirmed URL (e.g. a GitHub PR link) alongside plain references with no confirmed URL. Only use an entry to inform root_cause_narrative if this incident's error type/raw error lines above genuinely match that error_code's signature — a past cause for a DIFFERENT error code or engine does not apply here. If nothing matches, ignore this section and say so in reasoning rather than forcing a fit.
LINKING RULE — MANDATORY, do not skip this: a matching entry's optional confirmed_links field is a dict mapping citation text to its exact URL — the ONLY source of truth for which citations may be hyperlinked. Before you write root_cause_narrative, check confirmed_links: for every key that appears in it (e.g. "PR #10693"), your numbered finding MUST render it as a Slack link using that exact URL: <exact-url|PR #10693> — never just the bare text "PR #10693" when a link is available for it. For any OTHER PR/fix/ticket number mentioned in cause or fix_reference that is NOT a key in confirmed_links, cite it as plain text only (e.g. "PR #10709") — do not link it, and do not construct a github.com/.../pull/NNNN URL yourself just because a sibling PR in the same entry has one; a plausible-looking guessed URL is exactly the kind of fabrication this system exists to prevent. No confirmed_links field at all means nothing in that entry gets linked.)
{_format_past_cases(past_cases)}

TASK
Decide:
1. should_post — is this evidence strong enough to post (e.g. do we have a real failing engine, a sample error, an org)? If evidence is too thin/ambiguous, set false and explain why in reasoning.
2. root_cause_narrative — formatted as:
     Root cause:
     1. <finding>
     2. <finding>
     3. <finding>
   Each numbered line is one concrete finding (what failed, why, what fixed/would fix it, current status/deploy caveat) — short and scannable for a team reviewing the thread, not one flowing paragraph. Apply the LINKING RULE above to any PR/fix reference. Empty string if the evidence doesn't support a confident explanation and nothing in KNOWN ROOT CAUSES matches.
3. owning_team_mention — which team (as an "@team" mention) should be notified, based on the error type and how similar past incidents were routed. Empty string if unclear.
4. posts — the ordered list of Slack thread replies (top-level "Alert:" line is handled separately by the caller, do not include it).

FORMAT — this is the established #comms-noc look for engine-failure incidents, not optional or up to your taste. Every identifier value (engine name, engine ID, org ID, error type, TDO) is wrapped in single backticks, one field per line — never combine multiple fields into one flowing paragraph. Produce exactly these posts, in this order:
  1. "Engine: `<engine name>`" then on its own line "Engine ID: `<engine id>`" then on its own line "In the last <window>, <failed> (<failed_pct>%) tasks failed." — attach screenshot_tasks/screenshot_engine here if available.
  2. "Traffic from: org `<org id>`" (or "org <org name> (`<org id>`)" if the org name is meaningful) then on its own line "Error Type: `<error type>`" — attach the other screenshot here if available.
  3. The raw error, copied character-for-character from "Raw error lines" above, inside a triple-backtick code block. Copy it EXACTLY as given as one unbroken blob — do not reformat it, do not pretty-print it, and do not turn any `\n` you see into an actual line break; if the source text contains the two characters backslash-n, the code block must contain those same two characters, not a newline. Nothing else in this post.
  4. "TDO: `<tdo>`" then a short note that task/job logs are attached — attach task_log/job_log here.
  5. root_cause_narrative as plain prose (your judgment, grounded in the evidence/history/known-causes above) — no backtick-field formatting needed here, this one reads like an engineer explaining, not a data dump.
  6. The escalation line: owning_team_mention (if any) followed by "FYI^^", and nothing else in that post.
Omit a post only if it has nothing to say (e.g. no TDO found, or owning_team_mention is empty — then skip post 6 entirely rather than posting "FYI^^" alone).

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
    past_cases: Optional[List[dict]] = None,
) -> Decision:
    allowed_keys = ALLOWED_EVIDENCE_KEYS | set(extra_evidence_keys or [])
    prompt = _build_prompt(
        incident, evidence, history, available_evidence_keys, tdo_id,
        extra_evidence_descriptions, past_cases,
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

    return _validate_and_parse(output, incident, allowed_keys, evidence, past_cases)


_URL_PATTERN = re.compile(r"https?://[^\s|>)\]]+")


def _normalize_url(url: str) -> str:
    """Trim punctuation prose leaves on a URL, and any trailing slash."""
    return url.rstrip(".,;:!?)]}>|\"'").rstrip("/")


def _urls_in_evidence(incident: VictorOpsIncident,
                      evidence: Optional[EdgeUiTaskEvidence],
                      past_cases: Optional[List[dict]] = None) -> set:
    """Every URL the model was actually shown, normalized for comparison."""
    corpus = [incident.slack_permalink or "", incident.state_message or ""]
    if evidence is not None:
        corpus.append(" ".join(evidence.error_log_lines or []))
        corpus.append(evidence.error_type or "")
    # Every URL anywhere in a known_root_causes entry is real (either a
    # source_permalink captured from an actual tool call, or a fix/PR link
    # confirmed by rereading the source thread — see this agent's own
    # data/cases.json's _note). Scanning the whole cause dict, not just
    # source_permalink, means a real GitHub link embedded in fix_reference is
    # allowed too, without needing a dedicated field for every kind of link
    # that might show up in a future case.
    for case in past_cases or []:
        for cause in case.get("known_root_causes") or []:
            corpus.append(json.dumps(cause))
    found = set()
    for text in corpus:
        found.update(_normalize_url(u) for u in _URL_PATTERN.findall(text or ""))
    return found


def _validate_and_parse(
    output: dict, incident: VictorOpsIncident, allowed_keys: Optional[set] = None,
    evidence: Optional[EdgeUiTaskEvidence] = None, past_cases: Optional[List[dict]] = None,
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
    allowed_urls = _urls_in_evidence(incident, evidence, past_cases)
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
