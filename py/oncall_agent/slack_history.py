import re
from dataclasses import dataclass
from typing import List, Optional

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

"""
Reads past #comms-noc resolution threads so the LLM decision step can ground
its root-cause/escalation call in how humans actually handled similar
incidents before, instead of a hardcoded heuristic.

Read-only. Requires the bot token to have `channels:history` (public) or
`groups:history` (private) — the existing token (scopes: chat:write,
files:write only, confirmed 2026-08-24) does NOT have these yet, and if
#comms-noc is a private channel the bot also needs to be invited to it.
Both are Slack-admin actions only the user can take; this module degrades
gracefully (returns an empty list + a reason) rather than crashing, so the
rest of the pipeline can still run without historical grounding until that's
done.
"""

COMMS_NOC_CHANNEL_ID = "C01F810QM96"


@dataclass
class HistoricalThread:
    permalink: str
    top_level_text: str
    reply_texts: List[str]


@dataclass
class HistoryFetchResult:
    threads: List[HistoricalThread]
    unavailable_reason: Optional[str]


def fetch_relevant_comms_noc_history(
    client: WebClient,
    fingerprint_keywords: List[str],
    channel_id: str = COMMS_NOC_CHANNEL_ID,
    max_messages_scanned: int = 200,
    max_threads: int = 5,
) -> HistoryFetchResult:
    try:
        history = client.conversations_history(channel=channel_id, limit=max_messages_scanned)
    except SlackApiError as e:
        reason = e.response.get("error", str(e))
        return HistoryFetchResult(threads=[], unavailable_reason=f"conversations.history failed: {reason}")

    keywords_lower = [k.lower() for k in fingerprint_keywords]
    matching = [
        m
        for m in history.get("messages", [])
        if m.get("text") and any(k in m["text"].lower() for k in keywords_lower)
    ]
    matching = matching[:max_threads]

    threads: List[HistoricalThread] = []
    for msg in matching:
        thread_ts = msg.get("thread_ts") or msg.get("ts")
        reply_texts: List[str] = []
        try:
            replies = client.conversations_replies(channel=channel_id, ts=thread_ts, limit=50)
            reply_texts = [r["text"] for r in replies.get("messages", [])[1:] if r.get("text")]
        except SlackApiError:
            pass  # keep the top-level text even if replies can't be fetched

        try:
            permalink_res = client.chat_getPermalink(channel=channel_id, message_ts=thread_ts)
            permalink = permalink_res.get("permalink", "")
        except SlackApiError:
            permalink = ""

        threads.append(
            HistoricalThread(permalink=permalink, top_level_text=msg["text"], reply_texts=reply_texts)
        )

    return HistoryFetchResult(threads=threads, unavailable_reason=None)


ALERTS_DEVOPS_CHANNEL_ID = "C909ZH4ET"


def resolve_channel_id(client: WebClient, name_or_id: str) -> str:
    """Accept either a channel id (C…/G…) or a bare name and return the id.

    .env already stores SLACK_CHANNEL as a raw id, while humans write the
    channel by name — supporting both keeps either style working. Names are
    resolved by paging conversations.list, which needs channels:read /
    groups:read; an id passes straight through and needs nothing.
    """
    value = (name_or_id or "").lstrip("#").strip()
    if not value:
        raise RuntimeError("No channel configured")
    if re.fullmatch(r"[CGD][A-Z0-9]{6,}", value):
        return value

    cursor = None
    while True:
        res = client.conversations_list(
            limit=200, cursor=cursor, exclude_archived=True,
            types="public_channel,private_channel",
        )
        for channel in res.get("channels", []):
            if channel.get("name") == value:
                return channel["id"]
        cursor = (res.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    raise RuntimeError(
        f"Channel #{value} is not visible to the bot. Invite the app to it, or put "
        f"the channel ID in .env instead of the name (ids need no channels:read scope)."
    )


def fetch_channel_context(
    client: WebClient,
    channel_id: str,
    limit: int = 50,
    oldest: Optional[str] = None,
) -> HistoryFetchResult:
    """Recent top-level messages of a channel, as correlation context.

    Used for #alerts-devops: per DESIGN.md §1.5 the same real-world condition
    arrives two or three times (local + central Alertmanager, plus the
    VictorOps mirror), so what else was firing around an incident is real
    diagnostic signal. Read-only, and degrades to an empty result with a
    reason rather than raising — historical grounding is a bonus, not a
    prerequisite for investigating.
    """
    try:
        history = client.conversations_history(channel=channel_id, limit=limit, oldest=oldest)
    except SlackApiError as e:
        reason = e.response.get("error", str(e))
        return HistoryFetchResult(threads=[], unavailable_reason=f"conversations.history failed: {reason}")

    threads = [
        HistoricalThread(permalink="", top_level_text=m["text"], reply_texts=[])
        for m in history.get("messages", [])
        if m.get("text")
    ]
    return HistoryFetchResult(threads=threads, unavailable_reason=None)
