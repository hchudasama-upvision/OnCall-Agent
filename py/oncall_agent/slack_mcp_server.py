#!/usr/bin/env python3
"""
A read-only Slack MCP server, so a HEADLESS `claude -p` can read the channels
itself instead of being handed a keyword-prefetched digest.

Why this exists
---------------
Interactive Claude reaches Slack through the claude.ai connector, as the
signed-in user. A subprocess does not: `claude -p --allowed-tools
"mcp__claude_ai_Slack__slack_read_channel"` answers NO_SLACK_TOOLS (verified
2026-08-24). The connector is bound to the interactive session.

But the CLI does accept `--mcp-config`, so the same investigation can run
unattended against a server we own. That is this file. It exposes exactly
four read-only tools, no writes: the agent posts through the bot token in
slack_post.py, and nothing reachable from a prompt can send a message.

Token
-----
  SLACK_USER_TOKEN (xoxp-…)  preferred. Reads as the user, which is what
      "Claude can read those channels" means underneath — same access the
      connector has, including search.read.
  SLACK_BOT_TOKEN  (xoxb-…)  fallback. Needs channels:history (and
      groups:history for private channels); search is NOT available to bot
      tokens at all, so search_messages degrades to an explicit error rather
      than silently returning nothing.

Run it directly to sanity-check the wiring:
    python -m oncall_agent.slack_mcp_server --selftest
"""
import json
import time
import re
import os
import sys
from typing import Any, Dict, List, Optional

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "noc-slack", "version": "0.1.0"}

# Default channel ids so the model can say "read #comms-noc" without knowing
# the id, and so a typo cannot silently point the investigation elsewhere.
CHANNELS = {
    "#alerts-devops": "C909ZH4ET",
    "alerts-devops": "C909ZH4ET",
    "#comms-noc": "C01F810QM96",
    "comms-noc": "C01F810QM96",
}

_client = None


def _slack():
    """The Slack client, built once, from a user token where one exists."""
    global _client
    if _client is None:
        from slack_sdk import WebClient

        token = os.environ.get("SLACK_USER_TOKEN") or os.environ.get("SLACK_BOT_TOKEN")
        if not token:
            raise RuntimeError("Neither SLACK_USER_TOKEN nor SLACK_BOT_TOKEN is set")
        _client = WebClient(token=token)
    return _client


def _is_user_token() -> bool:
    return bool(os.environ.get("SLACK_USER_TOKEN"))


def _channel_id(value: str) -> str:
    return CHANNELS.get((value or "").strip(), (value or "").strip())


def _render_messages(messages: List[dict], channel: str) -> str:
    """Flatten to plain text — the model reads this, so shape it for reading.

    Deliberately includes the ts of every message: without it the model
    cannot ask for a thread, and it must never guess one.
    """
    from .alert_parser import flatten_message

    out: List[str] = []
    for m in messages:
        body = flatten_message(m).strip()
        if not body:
            continue
        who = m.get("user") or m.get("username") or m.get("bot_id") or "unknown"
        replies = m.get("reply_count") or 0
        thread_note = f" [thread: {replies} replies, read with ts={m.get('ts')}]" if replies else ""
        out.append(f"[ts={m.get('ts')} from={who}]{thread_note}\n{body}")
    if not out:
        return f"(no readable messages in {channel})"
    return "\n\n---\n\n".join(out)


# --------------------------------------------------------------------- tools

def tool_read_channel(channel: str, limit: int = 30, oldest: Optional[str] = None) -> str:
    channel_id = _channel_id(channel)
    res = _slack().conversations_history(channel=channel_id, limit=min(int(limit), 100), oldest=oldest)
    return _render_messages(res.get("messages", []), channel_id)


def tool_read_thread(channel: str, thread_ts: str, limit: int = 50) -> str:
    channel_id = _channel_id(channel)
    res = _slack().conversations_replies(channel=channel_id, ts=thread_ts, limit=min(int(limit), 200))
    return _render_messages(res.get("messages", []), channel_id)


def tool_search_messages(query: str, count: int = 20) -> str:
    if not _is_user_token():
        raise RuntimeError(
            "search requires SLACK_USER_TOKEN (xoxp-…) with search:read — the Slack API "
            "does not offer search to bot tokens at all. Use read_channel + read_thread "
            "instead, or set SLACK_USER_TOKEN."
        )
    res = _slack().search_messages(query=query, count=min(int(count), 100), sort="timestamp")
    matches = (res.get("messages") or {}).get("matches") or []
    if not matches:
        return f"(no matches for {query!r})"
    lines = []
    for m in matches:
        channel = (m.get("channel") or {}).get("name") or (m.get("channel") or {}).get("id") or "?"
        lines.append(
            f"[ts={m.get('ts')} channel=#{channel} from={m.get('username') or m.get('user') or '?'}]\n"
            f"{(m.get('text') or '').strip()}"
        )
    return "\n\n---\n\n".join(lines)


def tool_list_channels() -> str:
    known = "\n".join(f"  {name} -> {cid}" for name, cid in sorted(set(CHANNELS.items())))
    return ("Known channels (pass either the name or the id):\n" + known +
            f"\n\nToken in use: {'user (xoxp)' if _is_user_token() else 'bot (xoxb)'}")


# Channels where planned work is announced — deploys, maintenance windows, ITSM
# change records. Empty by default on purpose: reading the WRONG channel and
# reporting "no change found" is worse than saying the check was not configured,
# and PROFILE=test deliberately ships no channel defaults anywhere (CLAUDE.md
# non-negotiable #5).
CHANGE_CHANNELS = [c.strip() for c in os.environ.get("CHANGE_CHANNELS", "").split(",")
                   if c.strip()]


def tool_search_recent_changes(query: str, hours: int = 24, limit: int = 8) -> str:
    """Was planned work happening around this alert?

    Reads the CHANGE_CHANNELS (deploys / maintenance / ITSM change records) and
    returns the recent messages that mention `query` — a hostname, endpoint,
    service or cluster. This is the check a NOC engineer does before treating an
    EndpointDown as an incident: a Jenkins endpoint that is down during its own
    scheduled maintenance is not an outage.

    Deliberately NOT a verdict. It returns what those channels actually say, or
    says plainly that it could not read them. "No match" is not proof that no
    change is happening — it is the absence of an announcement in these
    channels, and the thread must say it that way.
    """
    if not CHANGE_CHANNELS:
        return ("CHANGE_CHANNELS is not configured, so no change/maintenance channel was "
                "searched. Do not conclude anything about planned work — say the check was "
                "unavailable. (Set CHANGE_CHANNELS in .env, e.g. '#prod-deploy,#change-mgmt'.)")

    oldest = str(time.time() - max(1, int(hours)) * 3600)
    needle = (query or "").strip().lower()
    # A hostname is the useful needle, not the whole URL: a deploy message says
    # "jenkins" or "us-1", never "https://jenkins.us-1.veritone.com/?x=1".
    tokens = {tok for tok in re.split(r"[^a-z0-9.-]+", needle) if len(tok) >= 4}
    if needle:
        tokens.add(needle)

    found, unreachable = [], []
    for channel in CHANGE_CHANNELS:
        try:
            channel_id = _channel_id(channel)
            res = _slack().conversations_history(channel=channel_id, limit=200, oldest=oldest)
        except Exception as e:                          # noqa: BLE001 — report, keep going
            unreachable.append(f"{channel} ({str(e).splitlines()[0][:120]})")
            continue
        for message in res.get("messages", []):
            text = (message.get("text") or "")
            if tokens and not any(tok in text.lower() for tok in tokens):
                continue
            found.append((channel, message.get("ts", ""), " ".join(text.split())[:300]))

    lines = []
    if found:
        lines.append(f"{len(found)} message(s) in the last {hours}h mentioning {query!r}:")
        for channel, ts, text in found[:max(1, int(limit))]:
            lines.append(f"  [{channel} ts={ts}] {text}")
        if len(found) > limit:
            lines.append(f"  ... {len(found) - limit} more not shown.")
        lines.append("These are announcements, not confirmation that the change caused this. "
                     "Quote them as what the channel says.")
    else:
        lines.append(f"No message in the last {hours}h in {', '.join(CHANGE_CHANNELS)} mentions "
                     f"{query!r}. That is the absence of an announcement in those channels — "
                     f"not proof that no change is under way. Say it that way.")
    if unreachable:
        lines.append("COULD NOT READ: " + "; ".join(unreachable)
                     + " — report this as a gap; do not treat it as 'no change found'.")
    return "\n".join(lines)


TOOLS = [
    {
        "name": "search_recent_changes",
        "description": (
            "Was planned work announced around this alert? Searches the configured change/"
            "deploy/maintenance channels (CHANGE_CHANNELS) for recent messages mentioning a "
            "host, endpoint, service or cluster. Use it before calling an EndpointDown an "
            "incident — and report 'no announcement found' as exactly that, never as 'no "
            "change is happening'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "host/endpoint/service to look for"},
                "hours": {"type": "integer", "description": "how far back (default 24)"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
        "handler": tool_search_recent_changes,
    },
    {
        "name": "read_channel",
        "description": (
            "Read recent messages from a Slack channel, newest first. Use '#comms-noc' "
            "to see how the NOC handled past incidents and '#alerts-devops' to see what "
            "else was firing at the same time. Every message is returned with its ts, "
            "which is what read_thread needs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel name (e.g. '#comms-noc') or id"},
                "limit": {"type": "integer", "description": "How many messages (max 100, default 30)"},
                "oldest": {"type": "string", "description": "Only messages after this Slack ts"},
            },
            "required": ["channel"],
        },
        "handler": tool_read_channel,
    },
    {
        "name": "read_thread",
        "description": (
            "Read one thread in full: the parent message and every reply. This is where "
            "the actual investigation lives in #comms-noc — what was checked, what was "
            "done, and who it was escalated to. Requires the parent message's ts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string"},
                "thread_ts": {"type": "string", "description": "ts of the PARENT message"},
                "limit": {"type": "integer"},
            },
            "required": ["channel", "thread_ts"],
        },
        "handler": tool_read_thread,
    },
    {
        "name": "search_messages",
        "description": (
            "Search Slack. Supports the usual modifiers: in:#comms-noc, from:@user, "
            "before:/after:YYYY-MM-DD, \"exact phrase\". The fastest way to find how an "
            "alert type was handled before. Needs a user token."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "count": {"type": "integer"},
            },
            "required": ["query"],
        },
        "handler": tool_search_messages,
    },
    {
        "name": "list_channels",
        "description": "The channel names/ids this server knows, and which token is in use.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_list_channels,
    },
]

_HANDLERS = {t["name"]: t["handler"] for t in TOOLS}
_TOOL_SPECS = [{k: v for k, v in t.items() if k != "handler"} for t in TOOLS]


# ---------------------------------------------------------------- jsonrpc io

def _result(request_id: Any, payload: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle(request: dict) -> Optional[dict]:
    """One JSON-RPC request in, one response out (None for notifications)."""
    method = request.get("method")
    request_id = request.get("id")

    if method == "initialize":
        return _result(request_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
    if method in ("notifications/initialized", "initialized"):
        return None                     # notification: no reply, ever
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": _TOOL_SPECS})
    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        handler = _HANDLERS.get(name)
        if not handler:
            return _error(request_id, -32602, f"Unknown tool: {name}")
        try:
            text = handler(**(params.get("arguments") or {}))
        except Exception as e:          # noqa: BLE001 — surface it to the model, don't die
            # isError lets the model see the failure and adapt (e.g. fall back
            # from search to read_channel) instead of the run aborting.
            return _result(request_id, {
                "content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}],
                "isError": True,
            })
        return _result(request_id, {"content": [{"type": "text", "text": text}]})

    if request_id is None:
        return None                     # unknown notification: ignore
    return _error(request_id, -32601, f"Method not found: {method}")


def serve() -> None:
    """Line-delimited JSON-RPC over stdio.

    stdout carries protocol only — anything else corrupts the stream, which is
    why every diagnostic in this file goes to stderr.
    """
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        try:
            response = handle(request)
        except Exception as e:          # noqa: BLE001
            response = _error(request.get("id"), -32603, f"{type(e).__name__}: {e}")
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


def _selftest() -> int:
    print(f"tools: {[t['name'] for t in TOOLS]}", file=sys.stderr)
    print(f"token: {'user (xoxp)' if _is_user_token() else 'bot (xoxb)'}", file=sys.stderr)
    for name, args in (
        ("list_channels", {}),
        ("read_channel", {"channel": "#alerts-devops", "limit": 2}),
        ("search_messages", {"query": "in:#comms-noc Alert", "count": 2}),
    ):
        try:
            out = _HANDLERS[name](**args)
            print(f"\n=== {name} OK ===\n{out[:600]}", file=sys.stderr)
        except Exception as e:          # noqa: BLE001
            print(f"\n=== {name} FAILED === {type(e).__name__}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    serve()
