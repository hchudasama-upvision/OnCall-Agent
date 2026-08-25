import json
import re
import subprocess
from dataclasses import dataclass
from typing import Optional

from .types import EdgeUiTaskEvidence, VictorOpsIncident

"""
The judgment step. What used to be a hardcoded heuristic (suggestOwningTeam)
is now decided by an LLM — shelled out to the `claude` CLI headlessly.

The message STRUCTURE (order of fields, screenshots, TDO, logs) stays a
fixed template (see composer.py) matching the NOC's own established
#comms-noc convention (confirmed against a real reference post 2026-08-24)
— this step decides only the judgment calls that convention doesn't encode:
whether the evidence is strong enough to post at all, the plain-language
root cause, and who to escalate to.

Historical grounding: the `claude` CLI is given its OWN read-only Slack MCP
tools (the "Claude" Slack connector, already granted channel-read access —
confirmed 2026-08-24) so it can look up past #comms-noc resolutions of
similar incidents itself. This pipeline's own Slack bot token
(oncall_agent/slack_post.py) is used ONLY for posting the final thread — it
is never used to read Slack. Two separate identities, two separate
responsibilities.

Hard guardrails (never delegated to the model):
  - Only read-only Slack tools are in --allowedTools — no write/send Slack
    tool exists on this connector to begin with, but this is enforced
    explicitly rather than relying on that.
  - The model never decides message structure, screenshots, or numbers —
    those come from composer.py filling in real evidence. It can only
    contribute root_cause_narrative and owning_team_mention text.
  - Output is validated against a JSON schema at the CLI layer
    (--json-schema); any missing/malformed field aborts rather than
    silently defaulting.
"""

COMMS_NOC_CHANNEL_ID = "C01F810QM96"

_SLACK_ARCHIVE_URL_RE = re.compile(r"^https://[\w.-]*\.slack\.com/archives/")
_GITHUB_URL_RE = re.compile(r"^https://github\.com/")
_URL_RE = re.compile(r"https?://\S+")

# Read-only Slack tools exposed by the "Claude" Slack connector (confirmed
# 2026-08-24 via `claude -p` probe) — no write/send tool is allowlisted here.
ALLOWED_SLACK_TOOLS = [
    "mcp__claude_ai_Slack__slack_read_channel",
    "mcp__claude_ai_Slack__slack_read_thread",
    "mcp__claude_ai_Slack__slack_search_channels",
    "mcp__claude_ai_Slack__slack_search_public",
]

_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "should_post": {"type": "boolean"},
        "reasoning": {
            "type": "string",
            "description": "Internal reasoning for the decision — never posted to Slack, audit-log only.",
        },
        "root_cause_narrative": {
            "type": "string",
            "description": "Short plain-language root-cause explanation, or empty string if not yet determinable from the evidence given.",
        },
        "owning_team_mention": {
            "type": "string",
            "description": "e.g. '@engines-team' or a specific person's name if that's who past #comms-noc threads show actually owns this — must be grounded in real history, not guessed. Empty string if unclear.",
        },
        "incident_permalink": {
            "type": "string",
            "description": "The real Slack permalink (https://...slack.com/archives/...) of THIS incident's own original alert message, if you found it while searching. Empty string if not found — never invent one.",
        },
        "is_known_recurring_issue": {
            "type": "boolean",
            "description": "True only if you found a real, still-open #comms-noc thread tracking this EXACT recurring engine/error signature across multiple past incidents.",
        },
        "existing_thread_permalink": {
            "type": "string",
            "description": "If is_known_recurring_issue is true: the real permalink of that thread's ROOT message (the original top-level alert that started the ongoing conversation), so this update can be appended there instead of starting a new thread. Empty string if is_known_recurring_issue is false or you couldn't find the root message.",
        },
        "is_exact_known_repeat": {
            "type": "boolean",
            "description": "Only meaningful when is_known_recurring_issue is true. True if THIS occurrence matches the already-diagnosed pattern with nothing new to report (same error signature, same kind of scope, nothing suggesting the cause changed) — a bare 'still happening' ping is enough, no need to re-explain or re-escalate what's already documented. False if you notice ANYTHING that doesn't fit the known pattern (different error detail, a scope/severity that looks new, timing that suggests fresh failures rather than the same stuck backlog, etc.) — that's worth full evidence + explanation because it might not actually be the same issue.",
        },
    },
    "required": [
        "should_post",
        "reasoning",
        "root_cause_narrative",
        "owning_team_mention",
        "incident_permalink",
        "is_known_recurring_issue",
        "existing_thread_permalink",
        "is_exact_known_repeat",
    ],
}


@dataclass
class Judgment:
    should_post: bool
    reasoning: str
    root_cause_narrative: str
    owning_team_mention: str
    incident_permalink: str
    is_known_recurring_issue: bool
    existing_thread_permalink: str
    is_exact_known_repeat: bool


def _build_prompt(incident: VictorOpsIncident, evidence: EdgeUiTaskEvidence, tdo_id: Optional[str]) -> str:
    return f"""You are making the judgment call on a real VictorOps engine-failure alert for the NOC team. The Slack message structure/format is fixed and handled separately — you are NOT writing the message, only deciding three things below, strictly grounded in real evidence and real Slack history — never invent facts, numbers, or people/teams not actually found.

FIRST: investigate history yourself. You have read-only Slack tools (slack_read_channel, slack_read_thread, slack_search_channels, slack_search_public). Read recent messages in #comms-noc (channel ID {COMMS_NOC_CHANNEL_ID}) and open threads for past incidents that mention "engine failure" or this engine/error type, to see how NOC engineers actually explained and routed similar incidents before.

ALSO: find this incident's own real Slack permalink. Search #comms-noc (and #alerts-devops if needed) for the literal text "Incident #{incident.incident_number}" — that's the original alert post this incident thread is about. If you find it, get its real permalink. If you can't find a message that is specifically about Incident #{incident.incident_number}, leave incident_permalink empty — do not substitute a different incident's link or invent one.

INCIDENT
  Number: {incident.incident_number}
  Title: {incident.incident_name}

EVIDENCE (real, gathered from Edge UI)
  Engine: {evidence.engine_name} (id {evidence.engine_id})
  Window: last {evidence.window_label}
  Totals: {evidence.total_tasks} tasks, {evidence.completed_tasks} completed ({evidence.completed_pct}%), {evidence.failed_tasks} failed ({evidence.failed_pct}%)
  Org: {evidence.scope_org_name} ({evidence.scope_org_id})
  Error type: {evidence.error_type}
  Raw error lines: {evidence.error_log_lines}
  TDO: {tdo_id or "(not found)"}

DECIDE
1. should_post — is this evidence strong enough to post (real failing engine, sample error, org)? If too thin/ambiguous, false + explain why in reasoning.
2. root_cause_narrative — short plain-language explanation of why this failed, based on the error type/raw error lines, informed by how past #comms-noc threads explained the SAME error signature if you found any (e.g. a linked PR/revert). If you cite a specific past message/person's call as your grounding (e.g. "per so-and-so's root-cause call"), you MUST fetch that exact message's real Slack permalink and embed it as a Slack-formatted link, e.g. "per <https://.../archives/...|Quynh Dang's root-cause call>" — never reference a thread by description alone without its real link. Empty string if not confidently determinable.
3. owning_team_mention — who should be @mentioned, grounded in who past #comms-noc threads show actually handled this exact engine/error before (a specific person's name if that's what history shows, otherwise a team). Empty string if you found no real grounding.
4. incident_permalink — this incident's own real Slack permalink if found (see above), else empty string.
5. is_known_recurring_issue — true only if you found a real, still-open #comms-noc thread already tracking this EXACT recurring engine/error signature (not just "similar errors happen sometimes" — the same signature recurring across multiple named past incidents in one continuous thread).
6. existing_thread_permalink — if is_known_recurring_issue is true, fetch and report the permalink of that thread's ROOT/first message (not a later reply) via slack_read_thread, so this update can be posted as a new reply in that SAME ongoing thread instead of starting a new one. Empty string otherwise.
7. is_exact_known_repeat — this is the one that needs real judgment, not a checklist. If is_known_recurring_issue is true, compare THIS occurrence's evidence against what you found in history: is it just the same already-diagnosed problem happening again with nothing new to say (→ true, a minimal ping suffices, no need to re-explain or re-tag anyone), or does something about it look different/new/worth a fresh look (→ false, this deserves full evidence and explanation, same as a first occurrence)? Default to false if you're not confident it's an exact repeat — a redundant full write-up is a much smaller mistake than silently glossing over something that turned out to be new.

Respond with ONLY the structured decision."""


def decide_resolution(
    incident: VictorOpsIncident,
    evidence: EdgeUiTaskEvidence,
    tdo_id: Optional[str],
    claude_binary: str = "claude",
    timeout_seconds: int = 480,
) -> Judgment:
    prompt = _build_prompt(incident, evidence, tdo_id)

    result = subprocess.run(
        [
            claude_binary,
            "-p",
            "--output-format",
            "json",
            "--allowedTools",
            *ALLOWED_SLACK_TOOLS,
            "--json-schema",
            json.dumps(_DECISION_SCHEMA),
            "--",
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

    owning_team_mention = output.get("owning_team_mention", "")
    if owning_team_mention and not owning_team_mention.startswith("@"):
        # The model sometimes returns a bare name/team ("Quynh Dang" instead
        # of "@Quynh Dang") — normalize rather than reject, since this is a
        # cosmetic formatting slip, not a grounding problem.
        owning_team_mention = f"@{owning_team_mention}"

    incident_permalink = output.get("incident_permalink", "")
    if incident_permalink and not _SLACK_ARCHIVE_URL_RE.match(incident_permalink):
        # A malformed/non-Slack URL here would be a fabrication, not a
        # grounding slip like the mention formatting above — refuse it
        # outright rather than pass it through to the composed message.
        raise RuntimeError(f"incident_permalink is not a real Slack archive URL, refusing to use it: {incident_permalink!r}")

    root_cause_narrative = output.get("root_cause_narrative", "")
    for url in _URL_RE.findall(root_cause_narrative):
        # Any URL the model cites must be either a real Slack thread link (the
        # grounding it was told to fetch) or a github.com link (plausible —
        # PR links like this appear verbatim inside real quoted Slack
        # message text, e.g. the image2pipe revert PR). Anything else is
        # unverifiable and refused rather than risk posting a fabricated URL.
        if not (_SLACK_ARCHIVE_URL_RE.match(url) or _GITHUB_URL_RE.match(url)):
            raise RuntimeError(f"root_cause_narrative contains an unverified URL, refusing to post: {url!r}")

    existing_thread_permalink = output.get("existing_thread_permalink", "")
    if existing_thread_permalink and not _SLACK_ARCHIVE_URL_RE.match(existing_thread_permalink):
        raise RuntimeError(
            f"existing_thread_permalink is not a real Slack archive URL, refusing to use it: {existing_thread_permalink!r}"
        )
    is_known_recurring_issue = bool(output.get("is_known_recurring_issue", False))
    if is_known_recurring_issue and not existing_thread_permalink:
        # Claiming "known recurring issue" without the thread to append to is
        # a half-finished decision — refuse rather than silently falling
        # back to a brand-new thread, since that's exactly the wrong
        # behavior this field exists to prevent.
        raise RuntimeError("is_known_recurring_issue=True but existing_thread_permalink is empty — refusing")

    return Judgment(
        should_post=bool(output.get("should_post", False)),
        reasoning=output.get("reasoning", ""),
        root_cause_narrative=root_cause_narrative,
        owning_team_mention=owning_team_mention,
        incident_permalink=incident_permalink,
        is_known_recurring_issue=is_known_recurring_issue,
        existing_thread_permalink=existing_thread_permalink,
        is_exact_known_repeat=bool(output.get("is_exact_known_repeat", False)) and is_known_recurring_issue,
    )


def parse_slack_permalink(permalink: str) -> tuple:
    """
    Extracts (channel_id, thread_ts) from a real Slack permalink, e.g.
    https://x.slack.com/archives/C01F810QM96/p1787271593526449?thread_ts=1787271593.526449&cid=C01F810QM96
    A root-message permalink has no thread_ts query param — in that case the
    message's own ts (derived from the p<digits> segment) IS the thread_ts.
    """
    match = re.search(r"/archives/([A-Z0-9]+)/p(\d+)", permalink)
    if not match:
        raise ValueError(f"Could not parse channel/ts out of Slack permalink: {permalink!r}")
    channel_id, ts_digits = match.group(1), match.group(2)

    thread_ts_match = re.search(r"thread_ts=([\d.]+)", permalink)
    if thread_ts_match:
        return channel_id, thread_ts_match.group(1)

    # p1787271593526449 -> 1787271593.526449 (last 6 digits are the fraction)
    thread_ts = f"{ts_digits[:-6]}.{ts_digits[-6:]}"
    return channel_id, thread_ts
