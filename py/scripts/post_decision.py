#!/usr/bin/env python3
"""
Post an investigated thread to #comms-noc from a decision JSON file.

This is the seam between investigating and posting, and it exists because the
two halves need different Slack credentials:

  READING  #alerts-devops and #comms-noc needs channels:history, which the bot
           token does not have. Claude has it — interactively, through the
           claude.ai Slack connector, as the signed-in user.
  WRITING  needs chat:write + files:write, which the bot token DOES have, and
           which Claude's connector should not be trusted with (a prompt that
           can read a channel and post to it is a prompt-injection target).

So: whoever can read investigates and writes the JSON; this script posts it.
That works today with no new Slack scopes. The unattended path
(oncall_agent/investigate.py, driven by scripts/run_listener.py) produces the
exact same JSON through the local Slack MCP server once a read-capable token
exists — one format, two producers.

Usage
  python py/scripts/post_decision.py decision.json                # dry run
  python py/scripts/post_decision.py decision.json --live
  python py/scripts/post_decision.py decision.json --live --thread-ts 1787568622.113189
  python py/scripts/post_decision.py --print-schema

Decision JSON
  {
    "alert": {                       # the incident being posted about
      "incident_number": 119884,
      "incident_name": "[FIRING:1] aiw-prod1001 : Engine failure rate above 15%",
      "incident_url": "https://portal.victorops.com/ui/wazee-digital-inc/incident/119884"
    },
    "should_post": true,
    "reasoning": "audit only, never posted",
    "prior_incidents": ["1787568742.149179"],
    "root_cause_narrative": "",
    "owning_team_mention": "<@U0AAJNGJG5U>",
    "posts": [
      {"text": "SI2 Playback segment creator, 21% of tasks failing since Aug 20.",
       "evidence_keys": []},
      {"text": "Known cause VE-26837 / PR #10693; revert merged as PR #10709.",
       "evidence_keys": ["screenshot_tasks"]}
    ],
    "evidence_files": {"screenshot_tasks": "dist/evidence/live-test-tasks-page.png"}
  }

--thread-ts replies into an EXISTING #comms-noc thread instead of opening a
new one. Use it when a human already posted the "Alert:" line — appending to
their thread is what recurrences do today; a second top-level post for the
same incident splits the conversation.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import bootstrap  # noqa: E402  — must precede third-party imports

bootstrap()

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from oncall_agent.config import load_config
from oncall_agent.slack_post import compose_top_level_text
from oncall_agent.types import VictorOpsIncident

SCHEMA_HINT = __doc__.split("Decision JSON")[1]


def _incident(alert: dict) -> VictorOpsIncident:
    return VictorOpsIncident(
        incident_number=int(alert.get("incident_number") or 0),
        organization=alert.get("organization") or "wazee-digital-inc",
        incident_name=alert.get("incident_name") or "",
        entity_display_name=alert.get("entity_display_name") or "",
        monitoring_tool=alert.get("monitoring_tool") or "",
        state_message=alert.get("state_message") or "",
        escalation_policy=alert.get("escalation_policy") or "",
        # The VictorOps portal URL is what humans put in the #comms-noc
        # "Alert:" line — not a Slack permalink.
        slack_permalink=alert.get("incident_url") or alert.get("slack_permalink") or "",
        created_at=alert.get("created_at") or "",
    )


def _resolve_files(decision: dict) -> dict:
    """evidence key -> existing Path. A named-but-missing file is fatal.

    Silently dropping it would post a reply whose text refers to a screenshot
    that is not there, which reads as evidence and is not.
    """
    files = {}
    for key, raw_path in (decision.get("evidence_files") or {}).items():
        path = Path(raw_path)
        if not path.exists():
            raise SystemExit(f"evidence_files[{key!r}] does not exist: {path}")
        files[key] = path
    referenced = {k for post in decision.get("posts", []) for k in (post.get("evidence_keys") or [])}
    missing = referenced - set(files)
    if missing:
        raise SystemExit(
            f"posts reference evidence keys with no file in evidence_files: {sorted(missing)}"
        )
    return files


def post(decision: dict, client, channel: str, thread_ts: str = "", live: bool = False) -> int:
    incident = _incident(decision.get("alert") or {})
    files = _resolve_files(decision)

    if not decision.get("should_post", False):
        print(f"should_post=false — nothing posted.\nReasoning: {decision.get('reasoning', '')}")
        return 0

    header = compose_top_level_text(incident)
    if not live:
        print(f"[DRY RUN] channel {channel}")
        if thread_ts:
            print(f"[DRY RUN] replying into existing thread {thread_ts} (no new top-level post)")
        else:
            print(f"[DRY RUN] would post top-level:\n{header}\n")
        for i, p in enumerate(decision.get("posts", []), 1):
            attached = [str(files[k]) for k in (p.get("evidence_keys") or [])]
            print(f"[DRY RUN] reply {i}:\n{p.get('text', '')}"
                  + (f"\n  files: {attached}" if attached else "") + "\n")
        prior = decision.get("prior_incidents") or []
        print(f"(grounded in {len(prior)} past thread(s): {prior})")
        print("(re-run with --live to post)")
        return 0

    if thread_ts:
        parent_ts = thread_ts
        print(f"replying into existing thread {parent_ts}")
    else:
        top = client.chat_postMessage(channel=channel, text=header)
        parent_ts = top["ts"]
        print(f"posted top-level {parent_ts}")

    for i, p in enumerate(decision.get("posts", []), 1):
        paths = [files[k] for k in (p.get("evidence_keys") or [])]
        if paths:
            client.files_upload_v2(
                channel=channel, thread_ts=parent_ts, initial_comment=p.get("text", ""),
                file_uploads=[{"file": str(x), "filename": x.name} for x in paths],
            )
            # files_upload_v2 resolving does not mean the file has rendered in
            # the channel yet; a plain reply sent right after can visibly land
            # above it.
            time.sleep(2)
        else:
            client.chat_postMessage(channel=channel, thread_ts=parent_ts, text=p.get("text", ""))
        print(f"posted reply {i}{' with ' + str(len(paths)) + ' file(s)' if paths else ''}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("decision", nargs="?", help="path to the decision JSON file")
    parser.add_argument("--live", action="store_true", help="actually post (default: dry run)")
    parser.add_argument("--channel", help="override the target channel id")
    parser.add_argument("--thread-ts", default="",
                        help="reply into this existing thread instead of opening a new one")
    parser.add_argument("--print-schema", action="store_true",
                        help="print the decision JSON format and exit")
    args = parser.parse_args()

    if args.print_schema:
        print(SCHEMA_HINT.strip())
        return 0
    if not args.decision:
        parser.error("a decision JSON path is required (or --print-schema)")

    load_dotenv()
    config = load_config()
    decision = json.loads(Path(args.decision).read_text())
    channel = args.channel or config.comms_channel

    client = None
    if args.live:
        if not config.slack_bot_token:
            raise SystemExit("SLACK_BOT_TOKEN is required to post")
        client = WebClient(token=config.slack_bot_token)
    try:
        return post(decision, client, channel, thread_ts=args.thread_ts, live=args.live)
    except SlackApiError as e:
        error = e.response.get("error", str(e))
        hint = {
            "not_in_channel": "invite the bot to the channel first",
            "channel_not_found": "wrong channel id, or the bot cannot see it",
            "missing_scope": "the bot token needs chat:write and files:write",
        }.get(error, "")
        raise SystemExit(f"Slack rejected the post: {error}" + (f" — {hint}" if hint else ""))


if __name__ == "__main__":
    sys.exit(main())
