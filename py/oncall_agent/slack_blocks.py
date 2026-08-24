from typing import List

"""
Slack message-shaping for long triage text. Ported from the noc-ai-lab
prototype together with the two size limits it cost real debugging to find:

  - one section block caps at 3000 chars;
  - the top-level `text` field caps at 4000 chars INDEPENDENTLY (verified:
    3999 accepted, 4001 -> msg_too_long). The 7-section report runs ~5.5k, so
    chat.update was rejected outright and the "triaging..." placeholder sat
    there forever while evidence posted around it — the bot looked hung
    mid-triage. With blocks present the `text` field is only notification
    fallback, so truncating it costs nothing.
"""

SECTION_LIMIT = 2900
TEXT_LIMIT = 3900


def fallback_text(text: str) -> str:
    """Notification/fallback text for a message whose blocks carry the content."""
    return text if len(text) <= TEXT_LIMIT else text[:TEXT_LIMIT] + "\n[...]"


def sections(text: str) -> List[str]:
    """Split triage text into section-sized chunks on paragraph boundaries."""
    chunks: List[str] = []
    for para in text.split("\n\n"):
        if chunks and len(chunks[-1]) + len(para) + 2 <= SECTION_LIMIT:
            chunks[-1] = f"{chunks[-1]}\n\n{para}"
        else:
            chunks.append(para)
    out: List[str] = []
    for chunk in chunks:            # one huge paragraph still has to be cut
        while len(chunk) > SECTION_LIMIT:
            cut = chunk.rfind("\n", 0, SECTION_LIMIT) + 1 or SECTION_LIMIT
            out.append(chunk[:cut])
            chunk = chunk[cut:]
        out.append(chunk)
    return [c for c in out if c.strip()] or ["(empty triage)"]


def triage_blocks(analysis: str, fingerprint: str, with_buttons: bool = False) -> List[dict]:
    """Triage text, optionally with the approve/deny controls.

    with_buttons is off unless the listener is running in Socket Mode: a
    button whose click nobody is listening for is worse than no button. Even
    when shown they are log-only — DESIGN.md §5 keeps remediation in
    deterministic code and there is no action-execution layer yet. When one
    lands, the approve handler is where a typed, guardrail-validated action
    gets built — not here, and not by the model.
    """
    blocks: List[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": chunk}}
        for chunk in sections(analysis)
    ]
    if with_buttons:
        blocks.append({
            "type": "actions", "block_id": "remediation",
            "elements": [
                {"type": "button", "action_id": "approve_remediation",
                 "style": "primary", "value": fingerprint[:2000],
                 "text": {"type": "plain_text", "text": "Approve"}},
                {"type": "button", "action_id": "deny_remediation",
                 "style": "danger", "value": fingerprint[:2000],
                 "text": {"type": "plain_text", "text": "Deny"}},
            ]})
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn",
             "text": "_No executor is wired — these buttons record a decision only._"}]})
    blocks.append({"type": "context", "elements": [
        {"type": "mrkdwn", "text": ":robot_face: _Posted by oncall-agent (automated triage)._"}]})
    return blocks
