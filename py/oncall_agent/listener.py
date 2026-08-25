import json
import time
from pathlib import Path
from typing import Callable, List, Optional

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from .alert_parser import parse_alert_message
from .config import AgentConfig, describe
from .handler import HandledAlert, SeenStore, handle_alert
from .slack_history import resolve_channel_id
from .types import ParsedAlert

"""
Watches #alerts-devops and hands every new alert to handler.handle_alert.

Two transports, because which one is available is a Slack-admin question,
not a code question:

  polling (default)  — conversations.history every POLL_SECONDS, cursored on
    the last message ts. Needs only the bot token this repo's .env already
    has, plus channels:history on #alerts-devops. Cannot receive button
    clicks.
  socket mode        — set SLACK_APP_TOKEN (xapp-…, scope connections:write)
    and the listener switches to slack_bolt over a websocket: real-time, no
    public URL, and interactive components work. Requires an app-level token
    from whoever administers the Slack app.

The dedupe store, suppression list and route selection live in handler.py
and are identical either way — the transport only decides how a message
arrives.
"""


def _log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


class Cursor:
    """Last processed message ts for #alerts-devops, persisted across restarts."""

    def __init__(self, path: Path):
        self.path = path
        self.value: Optional[str] = None
        if path.exists():
            try:
                self.value = json.loads(path.read_text()).get("last_ts")
            except (ValueError, OSError):
                self.value = None

    def advance(self, ts: str) -> None:
        if not ts or (self.value and float(ts) <= float(self.value)):
            return
        self.value = ts
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"last_ts": ts}))


def own_identities(client: WebClient, log=_log) -> set:
    """Every id Slack might stamp on a message this agent posts.

    auth.test returns both the bot's user_id and its bot_id; a message carries
    one or the other depending on how it was sent, so both are collected.
    Without this the agent reads its own reply, sees an alert-shaped message,
    and investigates itself — cheap to prevent, expensive to notice.
    """
    try:
        auth = client.auth_test()
    except SlackApiError as e:
        log(f"could not determine own identity ({e.response.get('error', e)}) — "
            f"self-loop protection is DEGRADED")
        return set()
    ids = {i for i in (auth.get("bot_id"), auth.get("user_id")) if i}
    log(f"own identities (never triaged): {sorted(ids)}")
    return ids


def resolve_channels(config: AgentConfig, client: WebClient, log=_log) -> AgentConfig:
    """Turn any channel names in config into ids, once, at startup."""
    for attr in ("alerts_channel", "comms_channel", "history_channel"):
        name = getattr(config, attr)
        try:
            setattr(config, attr, resolve_channel_id(client, name))
        except (RuntimeError, SlackApiError) as e:
            raise SystemExit(f"Cannot resolve {attr}={name!r}: {e}")
    log(f"config: {describe(config)}")
    return config


def fetch_new_messages(
    client: WebClient, config: AgentConfig, cursor: Cursor, log=_log
) -> List[dict]:
    """New top-level messages in #alerts-devops, oldest first.

    On a cold start (no cursor) it looks back LOOKBACK_MINUTES rather than
    replaying the whole channel — at dozens of alerts an hour, a full
    backfill would investigate hundreds of long-resolved incidents.
    """
    oldest = cursor.value or f"{time.time() - config.lookback_minutes * 60:.6f}"
    try:
        res = client.conversations_history(
            channel=config.alerts_channel, oldest=oldest, limit=200, inclusive=False
        )
    except SlackApiError as e:
        log(f"conversations.history failed: {e.response.get('error', e)}")
        return []
    messages = [
        m for m in res.get("messages", [])
        # Thread replies are ACK/RESOLVED updates on an existing card, not
        # new alerts; they reach us as correlation context, not triggers.
        if not m.get("thread_ts") or m.get("thread_ts") == m.get("ts")
    ]
    messages.sort(key=lambda m: float(m.get("ts", "0")))
    return messages


def process_message(
    message: dict, config: AgentConfig, client: Optional[WebClient],
    seen: SeenStore, log=_log, own_ids: Optional[set] = None,
) -> HandledAlert:
    alert: ParsedAlert = parse_alert_message(message, channel_id=config.alerts_channel)
    return handle_alert(alert, config, client, seen, log=log, own_ids=own_ids)


def run_once(config: AgentConfig, client: WebClient, seen: SeenStore,
             cursor: Cursor, log=_log, own_ids: Optional[set] = None) -> int:
    messages = fetch_new_messages(client, config, cursor, log=log)
    for message in messages:
        try:
            process_message(message, config, client, seen, log=log, own_ids=own_ids)
        except SystemExit as e:
            # The engine-failure pipeline aborts loudly (missing env, window
            # drift, no failing engine). That must kill the one alert, never
            # the listener.
            log(f"alert aborted: {e}")
        except Exception as e:                      # noqa: BLE001 — a daemon must not die
            log(f"alert failed: {type(e).__name__}: {e}")
        finally:
            cursor.advance(message.get("ts", ""))
    return len(messages)


def run_polling(config: AgentConfig, client: WebClient, log=_log) -> None:
    seen = SeenStore(config.state_dir / "seen.json", config.dedupe_window_minutes)
    cursor = Cursor(config.state_dir / "cursor.json")
    own_ids = own_identities(client, log=log)
    log(f"polling #{config.alerts_channel} every {config.poll_seconds}s "
        f"(cursor: {cursor.value or f'last {config.lookback_minutes}m'})")
    while True:
        count = run_once(config, client, seen, cursor, log=log, own_ids=own_ids)
        if count:
            log(f"processed {count} message(s)")
        time.sleep(config.poll_seconds)


def run_socket_mode(config: AgentConfig, client: WebClient, log=_log) -> None:
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    seen = SeenStore(config.state_dir / "seen.json", config.dedupe_window_minutes)
    own_ids = own_identities(client, log=log)

    # The alerts we care about are posted BY integrations, so Bolt's default
    # of dropping bot messages would drop everything. The self-check that
    # replaces it lives in handler.should_handle(own_ids=...) — mandatory now
    # that alerts_channel and comms_channel can be the same channel.
    app = App(token=config.slack_bot_token, ignoring_self_events_enabled=False)

    @app.event("message")
    def on_message(event, logger):                  # noqa: ARG001
        if event.get("channel") != config.alerts_channel:
            return
        if event.get("thread_ts") and event.get("thread_ts") != event.get("ts"):
            return
        if event.get("subtype") not in (None, "bot_message"):
            return                                  # edits/deletes/joins
        try:
            process_message(event, config, client, seen, log=log, own_ids=own_ids)
        except SystemExit as e:
            log(f"alert aborted: {e}")
        except Exception as e:                      # noqa: BLE001
            log(f"alert failed: {type(e).__name__}: {e}")

    _register_decision_buttons(app, log)

    log(f"socket mode connected — watching #{config.alerts_channel}")
    SocketModeHandler(app, config.slack_app_token).start()


def _register_decision_buttons(app, log=_log) -> None:
    """Approve/Deny record a decision and strip themselves. Nothing executes.

    DESIGN.md §5 keeps every state-changing action behind a typed,
    guardrail-validated action layer that does not exist yet. When it lands,
    the approve handler is where a validated action is built — never here,
    and never from model output.
    """
    from .slack_blocks import fallback_text

    def record(decision: str, body, client) -> None:
        user = body["user"]["id"]
        fingerprint = body["actions"][0].get("value") or "unknown"
        channel = body["container"]["channel_id"]
        ts = body["container"]["message_ts"]
        log(f"{decision.upper()} by {user} for {fingerprint} (message {ts})")
        kept = [b for b in body["message"]["blocks"] if b["type"] not in ("actions",)]
        kept.append({"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"*{decision.capitalize()}* by <@{user}> for `{fingerprint}` — "
                    f"recorded only, nothing was executed."}]})
        client.chat_update(channel=channel, ts=ts,
                           text=fallback_text(body["message"].get("text", "")), blocks=kept)

    @app.action("approve_remediation")
    def on_approve(ack, body, client):
        ack()
        record("approved", body, client)

    @app.action("deny_remediation")
    def on_deny(ack, body, client):
        ack()
        record("denied", body, client)


def run(config: AgentConfig, log=_log) -> None:
    if not config.slack_bot_token:
        raise SystemExit("SLACK_BOT_TOKEN is required to read #alerts-devops.")
    client = WebClient(token=config.slack_bot_token)
    config = resolve_channels(config, client, log=log)
    if config.post_mode == "dry_run":
        log("POST_MODE=dry_run — investigating for real, printing instead of posting. "
            "Set POST_MODE=live in .env to post to Slack.")
    if config.socket_mode:
        run_socket_mode(config, client, log=log)
    else:
        run_polling(config, client, log=log)
