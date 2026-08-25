#!/usr/bin/env python3
"""
The agent's main entry point: watch #alerts-devops, investigate, post to the
#comms-noc incident thread.

  python py/scripts/run_listener.py               # follow the channel (dry run by default)
  python py/scripts/run_listener.py --once        # one pass over new messages, then exit
  python py/scripts/run_listener.py --since 120   # cold start: look back 120 minutes
  python py/scripts/run_listener.py --live        # actually post to Slack (same as POST_MODE=live)
  python py/scripts/run_listener.py --message-url <slack permalink>   # replay one alert

--message-url is the one to reach for while tuning: it re-runs the full
investigation for a specific historical alert, bypassing the cursor and the
dedupe store, so you can compare the agent's thread against what the on-call
engineer actually wrote in #comms-noc that night.

Safety defaults (see oncall_agent/config.py): POST_MODE=dry_run, and only
VictorOps incidents trigger an investigation. Both are opt-out via .env.
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import bootstrap  # noqa: E402  — must precede third-party imports

bootstrap()

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from oncall_agent import listener
from oncall_agent.config import describe, load_config
from oncall_agent.handler import SeenStore

_PERMALINK = re.compile(r"/archives/(?P<channel>[A-Z0-9]+)/p(?P<ts>\d{10})(?P<micro>\d{6})")


def replay_message(url: str, config, log) -> int:
    """Re-investigate one specific alert, addressed by its Slack permalink."""
    m = _PERMALINK.search(url)
    if not m:
        raise SystemExit(f"Not a Slack message permalink: {url!r}")
    channel, ts = m.group("channel"), f"{m.group('ts')}.{m.group('micro')}"

    client = WebClient(token=config.slack_bot_token)
    config.alerts_channel = channel
    config = listener.resolve_channels(config, client, log=log)

    try:
        res = client.conversations_history(channel=channel, latest=ts, oldest=ts,
                                           inclusive=True, limit=1)
    except SlackApiError as e:
        # Every other read path in this repo degrades to "unavailable, here is
        # why"; this one used to let the raw SlackApiError escape as a
        # traceback, which reads like a crash rather than a missing scope.
        error = e.response.get("error", str(e))
        hint = {
            "missing_scope": (f"the token needs channels:history (groups:history for a "
                              f"private channel). It has: {e.response.get('provided', '?')}"),
            "not_in_channel": "invite the app to that channel first",
            "channel_not_found": "wrong channel id, or a private channel the token cannot see",
        }.get(error, "")
        raise SystemExit(f"Cannot read {channel}: {error}" + (f" — {hint}" if hint else ""))

    messages = res.get("messages", [])
    if not messages:
        raise SystemExit(f"No message at {ts} in {channel} — is the bot in that channel?")

    # A replay is an explicit human request for THIS alert, so the dedupe
    # store must not veto it: point it at a throwaway file rather than the
    # live one, which also keeps the replay from suppressing the real alert
    # if the listener picks it up later.
    seen = SeenStore(config.state_dir / "replay-seen.json", config.dedupe_window_minutes)
    seen.seen = {}
    result = listener.process_message(messages[0], config, client, seen, log=log)
    log(f"route={result.route} {result.reason}")
    return 0


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--once", action="store_true",
                        help="process new messages once and exit (good for cron)")
    parser.add_argument("--live", action="store_true",
                        help="post to Slack (overrides POST_MODE=dry_run)")
    parser.add_argument("--since", type=int, metavar="MINUTES",
                        help="cold-start lookback window (default LOOKBACK_MINUTES=15)")
    parser.add_argument("--message-url", metavar="URL",
                        help="replay one alert by its Slack permalink")
    parser.add_argument("--trigger-on", choices=("victorops", "all"),
                        help="what counts as a trigger (default: victorops incidents only)")
    args = parser.parse_args()

    config = load_config()
    if args.live:
        config.post_mode = "live"
    if args.since is not None:
        config.lookback_minutes = args.since
    if args.trigger_on:
        config.trigger_on = args.trigger_on

    log = listener._log
    if args.message_url:
        return replay_message(args.message_url, config, log)

    if not args.once:
        listener.run(config, log=log)
        return 0

    if not config.slack_bot_token:
        raise SystemExit("SLACK_BOT_TOKEN is required to read #alerts-devops.")
    client = WebClient(token=config.slack_bot_token)
    config = listener.resolve_channels(config, client, log=log)
    log(f"single pass: {describe(config)}")
    seen = SeenStore(config.state_dir / "seen.json", config.dedupe_window_minutes)
    cursor = listener.Cursor(config.state_dir / "cursor.json")
    count = listener.run_once(config, client, seen, cursor, log=log)
    log(f"processed {count} message(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
