import json
import subprocess
from typing import Dict, List, Optional, Tuple
from pathlib import Path

from .composer import ComposedThread, PlannedPost, compose_top_level_text
from .decide_resolution import (
    ALLOWED_SLACK_TOOLS,
    COMMS_NOC_CHANNEL_ID,
    _GITHUB_URL_RE,
    _SLACK_ARCHIVE_URL_RE,
    _URL_RE,
)
from .runscope_client import RunscopeEvidence, fetch_current_evidence
from .types import VictorOpsIncident

"""
Handles any VictorOps alert type that has no built SOP (no evidence-gathering
pipeline, no fixed template) — per 2026-08-24 direction: rather than guess a
generic format, investigate how NOC engineers actually handled this SAME
alert type before in #comms-noc, and mirror that. This is a deliberate
alternative to composer.py's fixed engine-failure template, which stays as
the validated path for that one alert type; this generic path only exists
for everything else.

For alert types backed by a real Runscope test (confirmed 2026-08-24 — the
"Site DNS Valdation" alert maps to a real test findable by name via the
public Runscope API), fetch the CURRENT failing run first: real evidence of
exactly which step/URL is failing right now and why, the same role Edge UI
evidence plays for engine-failure alerts. #comms-noc history is then used
only for root-cause/ownership grounding, not as a substitute for checking
what's actually happening now. Alert types with no matching Runscope test
fall back to history-only grounding, same as before.

The top-level "*Alert:* > Incident #N: <title>" line stays the one universal,
deterministic piece (DESIGN.md documents this as the NOC's consistent
practice across every alert type) — only the thread body is dynamically
discovered.
"""

_GENERIC_SCHEMA = {
    "type": "object",
    "properties": {
        "should_post": {
            "type": "boolean",
            "description": "False if you found nothing real to say — genuinely nothing known/documented about this alert type. Posting nothing is fine and often correct.",
        },
        "reasoning": {
            "type": "string",
            "description": "Internal reasoning — never posted, audit-log only.",
        },
        "found_prior_pattern": {
            "type": "boolean",
            "description": "True only if you found real past #comms-noc/#alerts-devops messages handling this SAME alert type/fingerprint (not just alerts in general).",
        },
        "reply_texts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Ordered Slack thread replies to post after the top-level Alert line (which is handled separately — do not include it). Empty array if should_post is false.",
        },
        "evidence_keys_used": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Any of the available evidence file keys you're attaching (e.g. a screenshot), if genuinely relevant. Empty array if none apply — never reference a key that wasn't given to you.",
        },
    },
    "required": ["should_post", "reasoning", "found_prior_pattern", "reply_texts", "evidence_keys_used"],
}


def _format_runscope_evidence(evidence: Optional[RunscopeEvidence]) -> str:
    if not evidence:
        return "No matching Runscope test found for this alert type — no current run evidence available; rely on #comms-noc history only, and be explicit that you couldn't verify the current state directly."

    if not evidence.failing_steps:
        return (
            f"REAL CURRENT EVIDENCE (Runscope test '{evidence.test_name}', run at {evidence.started_at}): "
            f"overall result is currently '{evidence.overall_result}' with {evidence.assertions_failed} failing assertion(s), "
            "but no specific failing step/URL detail was retrievable — treat the failure as unconfirmed-in-detail."
        )

    lines = [
        f"REAL CURRENT EVIDENCE (Runscope test '{evidence.test_name}', run at {evidence.started_at}) — "
        f"THIS is authoritative for what's happening right now, not the historical threads below:",
        f"  Overall: {evidence.overall_result}, {evidence.assertions_passed} passed / {evidence.assertions_failed} failed assertions",
    ]
    for step in evidence.failing_steps:
        lines.append(f"  Failing step: {step.url}")
        for a in step.failed_assertions:
            target = a.get("target_value")
            actual = str(a.get("actual_value", ""))[:300]
            lines.append(f"    assertion ({a.get('comparison')}): expected {target!r}, got: {actual!r}")
    return "\n".join(lines)


def _build_prompt(incident: VictorOpsIncident, available_evidence_keys: List[str], runscope_evidence: Optional[RunscopeEvidence]) -> str:
    evidence_note = (
        f"Evidence files available to attach (reference ONLY these exact keys in evidence_keys_used, never invent others): {available_evidence_keys}"
        if available_evidence_keys
        else "No evidence files (e.g. screenshots) are available for this alert type — evidence_keys_used must be empty."
    )

    return f"""You are handling a real VictorOps incident for the NOC team. This alert type has no pre-built template — your job is to determine what's ACTUALLY happening right now (using real current evidence if available) and how NOC engineers have actually handled this SAME kind of alert before in #comms-noc, then reply the way they did, using only real information.

{_format_runscope_evidence(runscope_evidence)}

SEPARATELY, for historical grounding (root cause pattern, ownership, tone): you have read-only Slack tools (slack_read_channel, slack_read_thread, slack_search_channels, slack_search_public). Search #comms-noc (channel ID {COMMS_NOC_CHANNEL_ID}) and #alerts-devops for past incidents matching this SAME alert fingerprint: "{incident.incident_name}". Read AT LEAST 5-10 matching past threads if that many exist (fewer only if fewer actually exist) before deciding — a single example isn't enough to be confident you've found the real pattern versus one unusual case. Open each matching thread (slack_read_thread) and see exactly how it was handled: what was said, what actions were taken, who was notified, what the resolution/status ended up being.

INCIDENT
  Number: {incident.incident_number}
  Title: {incident.incident_name}

{evidence_note}

DECIDE
1. should_post — is there real grounding (current evidence and/or history) to say something useful? Posting nothing is a completely valid and often correct answer if you have neither.
2. found_prior_pattern — did you find real #comms-noc/#alerts-devops history of this exact alert type?
3. reply_texts — the ordered thread replies to post (top-level "Alert:" line is handled separately, do not include it). Write the way real NOC engineers actually write these: short, direct bullet points with the specific technical facts highlighted in inline code (backticks) — e.g. the exact assertion text, hostname, status code, error string. If you have REAL CURRENT EVIDENCE above, lead with that — state definitively which step/URL is failing right now and why (this is the strongest, most useful thing you can say, since it's not a guess). Then use history to add root-cause pattern/context if it matches — but if the current evidence already tells you exactly what's wrong (e.g. it's the same known false-positive), say that plainly instead of presenting a hypothetical multi-branch decision tree. Do NOT list past incident/ticket numbers as a "signature dump" (like "#115343, #118037, #112926") — only cite ONE genuinely most-relevant prior thread if it adds real value, or none at all. Never invent facts beyond what the current evidence or a real historical thread actually shows.
4. evidence_keys_used — reference real evidence file keys only if genuinely relevant and available (see above); otherwise empty.

Respond with ONLY the structured decision."""


def investigate_and_compose(
    incident: VictorOpsIncident,
    evidence_file_paths: Optional[Dict[str, Path]] = None,
    existing_thread: Optional[Tuple[str, str]] = None,
    claude_binary: str = "claude",
    timeout_seconds: int = 480,
) -> ComposedThread:
    evidence_file_paths = evidence_file_paths or {}
    try:
        runscope_evidence = fetch_current_evidence(incident.incident_name)
    except Exception as e:
        # A Runscope API hiccup shouldn't block posting entirely — fall back
        # to history-only grounding rather than crash, but never silently
        # pretend nothing was checked.
        runscope_evidence = None
        print(f"Runscope evidence lookup failed ({e}) — falling back to history-only grounding")
    prompt = _build_prompt(incident, list(evidence_file_paths.keys()), runscope_evidence)

    result = subprocess.run(
        [
            claude_binary,
            "-p",
            "--output-format",
            "json",
            "--allowedTools",
            *ALLOWED_SLACK_TOOLS,
            "--json-schema",
            json.dumps(_GENERIC_SCHEMA),
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

    # Real URLs the model may legitimately state: the actual site(s) under
    # test right now (verifiable against real Runscope evidence), in
    # addition to Slack/GitHub citation links.
    verified_step_urls = {step.url for step in (runscope_evidence.failing_steps if runscope_evidence else [])}

    reply_texts = output.get("reply_texts", [])
    for text in reply_texts:
        for raw_url in _URL_RE.findall(text):
            # Inline-code-wrapped URLs (`https://...`) get a trailing
            # backtick swept up by \S+ — strip trailing punctuation before
            # checking, rather than reject a legitimately real URL over it.
            url = raw_url.rstrip("`.,)>\"'")
            if not (
                _SLACK_ARCHIVE_URL_RE.match(url)
                or _GITHUB_URL_RE.match(url)
                or url in verified_step_urls
                or any(url.startswith(step_url) for step_url in verified_step_urls)
            ):
                raise RuntimeError(f"Generic alert reply contains an unverified URL, refusing to post: {url!r}")

    evidence_keys_used = output.get("evidence_keys_used", [])
    bad_keys = [k for k in evidence_keys_used if k not in evidence_file_paths]
    if bad_keys:
        raise RuntimeError(f"Generic alert referenced unknown evidence keys {bad_keys} — refusing to post")

    posts = [PlannedPost(text=t, evidence_keys=[]) for t in reply_texts]
    if evidence_keys_used and posts:
        # Attach to the last reply — the natural place for "here's the
        # evidence" to sit relative to the preceding explanation.
        posts[-1].evidence_keys = evidence_keys_used

    return ComposedThread(
        top_level_text=compose_top_level_text(incident),
        should_post=bool(output.get("should_post", False)),
        posts=posts,
        existing_thread=existing_thread,
    )
