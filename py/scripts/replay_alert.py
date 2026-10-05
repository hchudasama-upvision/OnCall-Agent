#!/usr/bin/env python3
"""
Fire an alert at the agent without needing read access to Slack.

    python py/scripts/replay_alert.py --incident "<alert line>"     # POSTS to Slack
    python py/scripts/replay_alert.py --incident "<alert line>" --dry-run
    python py/scripts/replay_alert.py --list
    python py/scripts/replay_alert.py --fixture "pvc"
    python py/scripts/replay_alert.py --payload alert.txt --dry-run   # a REAL pasted card
    pbpaste | python py/scripts/replay_alert.py --payload - --dry-run

By default this DOES post: the alert card goes into the channel, the agent
investigates, and its findings + Grafana screenshots are posted as replies in
that alert's own thread. Pass --dry-run to investigate without touching Slack.

Why this exists
---------------
The normal path is: alert lands in #alerts-devops -> listener reads it ->
agent investigates. That first read needs `channels:history` on a token
belonging to the workspace the channel lives in. Today it has neither: the
bot token is `nocautomationbot @ UpVision` (upvision-in.slack.com) while
#alerts-devops and #comms-noc are on veritone.slack.com.

So this script skips the read. It hands the alert dict straight to
handler.handle_alert() — the exact same code path the listener uses once a
message arrives — which means everything downstream is genuinely exercised:
parsing, dedupe, suppression, routing, evidence gathering, the model call,
and the composed thread. Only the "fetch it from Slack" step is replaced.

  (no flags)  feed the alert in, print what would be posted. Touches Slack
              not at all. This works with an empty .env.
  --post      ALSO post the alert card itself into --alert-channel first, so
              the thread has something real to hang off and the demo looks
              like production. Needs chat:write, which the bot token has.
  --live      ALSO post the investigation. Everything else is a dry run.

--post is the piece that turns this into the noc-ai-lab replayer loop: a
visible alert in a channel, then a threaded investigation under it.
"""
import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import bootstrap  # noqa: E402  — must precede third-party imports

bootstrap()

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from oncall_agent import investigate
from oncall_agent.alert_parser import parse_alert_message
from oncall_agent.config import describe, load_config
from oncall_agent.handler import SeenStore, handle_alert
from oncall_agent.listener import _log

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "alerts-devops-real.json"
# The 2026-09 cards, whose state_message carries Summary/Description/Labels.
_FIXTURES_STATE = _FIXTURES.parent / "alerts-devops-state-message.json"


def load_fixtures() -> list:
    """Both fixture sets: the 2026-08 cards and the 2026-09 state_message ones.

    They are separate files because they are different PAYLOAD SHAPES, not just
    different alerts. The Alertmanager -> VictorOps template gained a
    per-sub-alert Summary/Description/Labels block between them, so the older
    set carries "state_message: *Alertmanager:* <url>" and nothing more. Both
    still arrive in production (a closed incident has its block replaced), so
    both have to keep parsing.
    """
    messages = list(json.loads(_FIXTURES.read_text())["messages"])
    if _FIXTURES_STATE.exists():
        messages += json.loads(_FIXTURES_STATE.read_text())
    return messages


def pick_fixture(needle: str) -> dict:
    matches = [m for m in load_fixtures() if needle.lower() in m["_case"].lower()]
    if not matches:
        raise SystemExit(f"No fixture matches {needle!r}. Use --list to see them.")
    if len(matches) > 1:
        names = "\n  ".join(m["_case"] for m in matches)
        raise SystemExit(f"{needle!r} matches {len(matches)} fixtures — be more specific:\n  {names}")
    return matches[0]


def _sub_alert_block(summary: str, description: str, labels: str, status: str) -> str:
    """A realistic state_message block for --incident.

    Without this, --incident synthesizes the PRE-2026-09 shape
    ("state_message: *Alertmanager:* <url>"), so a replay exercises a payload
    production no longer sends and the sub-alert path is never tested. The
    bullet is written HTML-escaped, as the real transmitter sends it.
    """
    lines = [f"&bull; *[{status.upper()}] alert*"]
    if summary:
        lines.append(f"  *Summary:* {summary}")
    if description:
        lines.append(f"  *Description:* {description}")
    if labels:
        lines.append("  *Labels:*")
        for pair in labels.split(","):
            key, _, value = pair.partition("=")
            if key.strip():
                lines.append(f"    - {key.strip()}: {value.strip()}")
    lines.append("  *Started:* 2026-09-11 10:19:47 UTC")
    return "\n".join(lines)


def synthesize(incident_line: str, number: int, org: str = "wazee-digital-inc",
               state_message: str = "") -> dict:
    """Build a VictorOps card around an arbitrary alert line.

    Mirrors the real payload shape exactly (see fixtures/alerts-devops-real.json):
    the linked heading carries the title, INCIDENT_NAME carries the NUMBER, and
    the transmitter's keys are lower-case inside the fence. Getting this wrong
    would make the replay test a parser that never sees production's format.
    """
    url = f"https://portal.victorops.com/ui/{org}/incident/{number}"
    return {
        "ts": f"{time.time():.6f}",
        "bot_id": "BCT64JZ16",
        "text": "",
        "attachments": [{
            "text": (
                f"*Organization:* {org}\n"
                f"*<{url}|Incident #{number}>: {incident_line}*\n"
                f"{incident_line}\n"
                f"```monitoring_tool: Alertmanager\n"
                f"entity_display_name: {incident_line}\n"
                f"state_message: {state_message or '*Alertmanager:* <http://thanos-alertmanager.ops.veritone.com>'}\n"
                f"CONTACTGROUPNAME: devops-oncall\n"
                f"CURRENT_ALERT_PHASE: FIRING\n"
                f"CURRENT_STATE: CRITICAL\n"
                f"INCIDENT_NAME: {number}\n"
                f"MONITOR_TYPE: UNKNOWN\n"
                f"SERVICE: {incident_line}\n```"
            )
        }],
    }


def post_alert_card(client: WebClient, channel: str, message: dict, log) -> str:
    """Put the alert itself in a channel so the demo has a real anchor."""
    attachment_text = (message.get("attachments") or [{}])[0].get("text", "")
    try:
        res = client.chat_postMessage(
            channel=channel,
            text=":rotating_light: (replayed alert)",
            attachments=[{"color": "#e01e5a", "text": attachment_text}],
        )
    except SlackApiError as e:
        error = e.response.get("error", str(e))
        hint = {
            "not_in_channel": "invite the bot to that channel first",
            "channel_not_found": ("wrong id, or the channel is in a different workspace than "
                                  "the bot token — check `python py/scripts/check_setup.py`"),
            "missing_scope": f"the token needs chat:write. It has: {e.response.get('provided', '?')}",
        }.get(error, "")
        raise SystemExit(f"Could not post the alert card to {channel}: {error}"
                         + (f" — {hint}" if hint else ""))
    log(f"posted alert card to {channel} at ts={res['ts']}")
    return res["ts"]


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--fixture", metavar="SUBSTRING",
                        help="replay a real captured alert (substring of its _case)")
    source.add_argument("--incident", metavar="LINE",
                        help='synthesize a card, e.g. "[FIRING:1] aiw-prd5001 : Engine failure rate above 15%%"')
    # The one that takes a REAL alert whole. --fixture replays a card someone
    # captured earlier and --incident synthesizes one around a title; neither
    # helps when you have the actual alert in front of you and want to run THAT.
    # Paste it into a file (or pipe it on stdin with "-") and it is used byte
    # for byte: no title parsing, no reconstruction, every sub-alert intact.
    source.add_argument("--payload", metavar="FILE",
                        help='replay a whole pasted alert card from a file, verbatim '
                             '("-" reads stdin). Use this for a real alert you have in hand.')
    parser.add_argument("--list", action="store_true", help="list available fixtures and exit")
    # The card's own Summary/Description/Labels. Production sends these on
    # every live Alertmanager incident; the agent now parses them into
    # sub-alerts and the specialists are told to read them before reaching for
    # a tool, so a replay without them tests the wrong shape.
    parser.add_argument("--summary", default="", help="*Summary:* line for --incident")
    parser.add_argument("--description", default="", help="*Description:* line for --incident")
    parser.add_argument("--labels-kv", default="", metavar="K=V,K=V",
                        help="named *Labels:* for --incident, e.g. "
                             "'node=ip-10-0-41-41.ec2.internal,zone=us-east-1a'")
    parser.add_argument("--sub-alert-status", default="FIRING", choices=("FIRING", "RESOLVED"),
                        help="status of the synthesized sub-alert (default FIRING)")
    # Unique per run: the audit record is written to .state/audit/<number>.json,
    # so a fixed default meant two terminals overwrote each other's record.
    parser.add_argument("--number", type=int, default=990000 + (os.getpid() % 9000),
                        help="incident number for --incident (default: unique per run)")
    # This is a MANUAL testing tool: you type an alert and fire it, so the
    # whole point is seeing it land in Slack. It posts by DEFAULT. (The
    # listener, which watches a real channel unattended, keeps POST_MODE=dry_run
    # as its default — that is where an accidental post actually matters.)
    parser.add_argument("--dry-run", action="store_true",
                        help="do NOT touch Slack — investigate and print only")
    parser.add_argument("--alert-channel",
                        help="where to post the replayed alert (default: the comms channel)")
    parser.add_argument("--no-post", action="store_true",
                        help="skip posting the alert card; only post the investigation")
    parser.add_argument("--trigger-on", choices=("victorops", "all"),
                        help="override what counts as a trigger")
    parser.add_argument("--labels", action="store_true",
                        help="print the alert's parsed labels + panel lookup and exit "
                             "(use it to pick {label:N} indices for config/panel_map.json)")
    args = parser.parse_args()

    if args.list:
        for m in load_fixtures():
            print(f"  {m['_case']}")
        return 0
    if not (args.fixture or args.incident or args.payload):
        parser.error("pass --fixture, --incident, or --list")

    if args.payload:
        raw = sys.stdin.read() if args.payload == "-" else Path(args.payload).read_text()
        if not raw.strip():
            raise SystemExit("--payload was empty — nothing to replay")
        # No wrapping and no cleanup. A pasted card is already the payload, and
        # the parser is built to cope with what a paste loses (the ``` fence,
        # the portal link, the trailing VictorOps metadata).
        message = {"ts": f"{time.time():.6f}", "bot_id": "BCT64JZ16",
                   "text": raw, "attachments": []}
    elif args.fixture:
        message = pick_fixture(args.fixture)
    else:
        block = ""
        if args.summary or args.description or args.labels_kv:
            block = _sub_alert_block(args.summary, args.description, args.labels_kv,
                                     args.sub_alert_status)
        message = synthesize(args.incident, args.number, state_message=block)

    config = load_config()
    post_alert = not args.dry_run and not args.no_post
    if not args.dry_run:
        config.post_mode = "live"
    if args.trigger_on:
        config.trigger_on = args.trigger_on
    _log(f"config: {describe(config)}")

    client = WebClient(token=config.slack_bot_token) if config.slack_bot_token else None
    alert_channel = args.alert_channel or config.comms_channel

    posted_ts = ""
    if post_alert:
        if not client:
            raise SystemExit("--post needs SLACK_BOT_TOKEN")
        # Check the model BEFORE the alert card goes out. An expired CLI login
        # would otherwise leave an alert sitting in the channel with no
        # investigation under it — the worst possible state to demo.
        try:
            investigate.preflight_llm()
            _log("llm reachable")
        except Exception as e:                      # noqa: BLE001
            raise SystemExit(f"Not posting: {e}")
        posted_ts = post_alert_card(client, alert_channel, message, _log)
        message["ts"] = posted_ts
        message["channel"] = alert_channel

    if args.labels:
        alert = parse_alert_message(message, channel_id=alert_channel)
        print(f"alert_name : {alert.alert_name!r}")
        print(f"env        : {alert.environment_key or '(none)'}")
        print("labels     : (index -> value; negative indices count from the end)")
        total = len(alert.labels)
        for i, value in enumerate(alert.labels):
            print(f"  {{label:{i}}} / {{label:{i - total}}}  {value}")
        print(f"hints      : {json.dumps(alert.label_hints) if alert.label_hints else '(none)'}")
        from oncall_agent.evidence_panels import load_panel_map
        specs = load_panel_map().specs_for(alert.alert_name, alert.environment_key,
                                           labels=alert.labels, hints=alert.label_hints)
        if specs:
            for spec in specs:
                print(f"panel      : {spec.label} vars={spec.variables} from={spec.from_}")
        else:
            print(f"panel      : no config/panel_map.json entry for {alert.alert_name!r}")
        return 0

    alert = parse_alert_message(message, channel_id=alert_channel)
    # The card we just posted IS the incident's top-level message, so the
    # investigation threads under it. Without this the agent posts its own
    # "Alert:" header too and the channel shows the same incident twice.
    if posted_ts and alert_channel == config.comms_channel:
        alert.reply_in_thread_ts = posted_ts
    _log(f"parsed: {alert.source}/{alert.kind} #{alert.incident_number} "
         f"{alert.alert_name!r} env={alert.environment_key or '-'} trigger={alert.is_trigger}")

    # A replay is an explicit request for THIS alert, so it gets a throwaway
    # dedupe store: replaying the same fixture twice in a row must work, and
    # must not poison the real listener's state.
    seen = SeenStore(Path(tempfile.mkdtemp()) / "seen.json", config.dedupe_window_minutes)
    result = handle_alert(alert, config, client, seen, log=_log)
    _log(f"route={result.route} {result.reason}")
    # An armed re-check (API response-code alerts) lives on a timer thread. A
    # one-shot replay would exit before it fires, so wait for it here — and for
    # testing, override the wait with e.g. FOLLOW_UP_MINUTES=0.2.
    from oncall_agent import follow_up
    import threading
    if any(th.name.startswith("followup-") for th in threading.enumerate()):
        _log(f"waiting for the armed follow-up re-check "
             f"(FOLLOW_UP_MINUTES={','.join(f'{m:g}' for m in follow_up.FOLLOW_UP_MINUTES)}) — "
             f"Ctrl-C to skip")
        try:
            follow_up.wait_for_pending()
        except KeyboardInterrupt:
            _log("skipped waiting for the follow-up (it is still recorded in .state/followups)")

    if args.dry_run:
        _log("(--dry-run: nothing was posted)")
    else:
        _log(f"posted to {alert_channel} — open Slack and look at the alert's thread")
    return 0


if __name__ == "__main__":
    sys.exit(main())
