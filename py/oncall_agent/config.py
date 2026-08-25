import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

"""
Runtime configuration, read once at startup.

Two defaults are chosen deliberately rather than for convenience:

  POST_MODE defaults to "dry_run". DESIGN.md §6 prescribes shadow mode
  before assist mode, and an agent that starts posting into a real NOC
  incident channel the first time someone runs it is exactly the failure
  this repo should not have. Live posting is an explicit opt-in.

  TRIGGER_ON defaults to "victorops" — the v1.1 scope refinement. Raw
  Alertmanager/PandoLogic/Jenkins posts in #alerts-devops are correlation
  context, not triggers; #alerts-devops is ~100% bot traffic at dozens of
  messages an hour and triggering on all of it would post more noise than
  the humans currently do.
"""

_ROOT = Path(__file__).resolve().parents[2]

# The real Veritone channels (DESIGN.md §1). The brief said "#coms-noc"/
# "alert-devops"; the actual names are #alerts-devops and #comms-noc.
PROD_ALERTS_CHANNEL = "C909ZH4ET"     # #alerts-devops, veritone.slack.com
PROD_COMMS_CHANNEL = "C01F810QM96"    # #comms-noc,    veritone.slack.com

# PROFILE decides whether those are the defaults.
#
#   test (default)  no channel defaults at all — ALERTS_CHANNEL and the post
#                   target must be named explicitly. Running against a test
#                   workspace is the normal case while this is being built.
#   production      defaults to the Veritone channels above.
#
# The defaults used to be the production channels unconditionally, which meant
# an .env that simply forgot ALERTS_CHANNEL aimed the agent at the real NOC
# channel — and `--live` would have posted there. Requiring the profile to be
# stated makes reaching production a decision rather than an oversight.
PROFILES = ("test", "production")


@dataclass
class AgentConfig:
    profile: str                         # test | production
    alerts_channel: str
    comms_channel: str
    history_channel: str
    post_mode: str                       # dry_run | live
    trigger_on: str                      # victorops | all
    poll_seconds: int
    lookback_minutes: int
    dedupe_window_minutes: int
    evidence_dir: Path
    state_dir: Path
    slack_bot_token: Optional[str]
    slack_app_token: Optional[str]
    slack_user_token: Optional[str]
    investigation: str = "auto"          # auto | tools | prefetch
    suppressed: List[re.Pattern] = field(default_factory=list)

    @property
    def can_read_slack(self) -> bool:
        """Is there a token that can actually read channel history?

        A user token always can. The bot token can only if channels:history
        was granted, which cannot be known without calling — so this is the
        optimistic answer and the callers degrade on the real error.
        """
        return bool(self.slack_user_token or self.slack_bot_token)

    def investigation_mode(self, mcp_config_exists: bool,
                           grafana_configured: bool = False) -> str:
        """tools = Claude reads #comms-noc itself; prefetch = we hand it threads.

        "auto" picks tools only when there is a user token behind the MCP
        server. Giving the model search tools it cannot authenticate wastes a
        multi-minute run to arrive at "I could not read anything", which the
        cheaper prefetch path reports immediately.
        """
        if self.investigation in ("tools", "prefetch"):
            return self.investigation
        if not mcp_config_exists:
            return "prefetch"
        # Either source of live evidence justifies the tool path: Slack history
        # (needs a user token) OR Grafana, which needs no Slack scope at all.
        # Gating on the user token alone kept the graph alerts — the whole
        # point of the tool path — on the offline route.
        return "tools" if (self.slack_user_token or grafana_configured) else "prefetch"

    @property
    def single_channel(self) -> bool:
        """Reading and posting in the SAME channel.

        Normal for a small test workspace with one channel. It makes two things
        mandatory that are otherwise optional: the agent must ignore messages it
        wrote itself (or it triages its own output forever), and it must reply
        inside the alert's thread rather than opening a new top-level message.
        """
        return self.alerts_channel == self.comms_channel

    @property
    def is_live(self) -> bool:
        return self.post_mode == "live"

    @property
    def socket_mode(self) -> bool:
        """Socket Mode needs an app-level token (xapp-…, scope connections:write).

        Without one the listener polls conversations.history instead, which
        works with the plain bot token this repo's .env already has. Polling
        cannot receive button clicks, which is why the approve/deny controls
        only render in Socket Mode.
        """
        return bool(self.slack_app_token)

    def is_suppressed(self, fingerprint: str) -> bool:
        return any(p.search(fingerprint or "") for p in self.suppressed)


def _load_suppression(path: Path) -> List[re.Pattern]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text()).get("suppressed_fingerprints") or []
    return [re.compile(p, re.IGNORECASE) for p in raw]


def load_config(env=None) -> AgentConfig:
    env = env if env is not None else os.environ
    post_mode = (env.get("POST_MODE") or "dry_run").lower()
    if post_mode not in ("dry_run", "live"):
        raise SystemExit(f"POST_MODE must be 'dry_run' or 'live', got {post_mode!r}")
    trigger_on = (env.get("TRIGGER_ON") or "victorops").lower()
    if trigger_on not in ("victorops", "all"):
        raise SystemExit(f"TRIGGER_ON must be 'victorops' or 'all', got {trigger_on!r}")

    profile = (env.get("PROFILE") or "test").lower()
    if profile not in PROFILES:
        raise SystemExit(f"PROFILE must be one of {PROFILES}, got {profile!r}")
    production = profile == "production"

    # SLACK_CHANNEL is this repo's existing name for "where the agent posts".
    comms = env.get("COMMS_CHANNEL") or env.get("SLACK_CHANNEL") or (
        PROD_COMMS_CHANNEL if production else "")
    alerts = env.get("ALERTS_CHANNEL") or (PROD_ALERTS_CHANNEL if production else "")
    if not alerts or not comms:
        missing = " and ".join(
            n for n, v in (("ALERTS_CHANNEL", alerts), ("SLACK_CHANNEL", comms)) if not v)
        raise SystemExit(
            f"PROFILE=test requires {missing} to be set explicitly in .env.\n"
            f"Set PROFILE=production to default to the real Veritone channels "
            f"({PROD_ALERTS_CHANNEL} / {PROD_COMMS_CHANNEL})."
        )

    return AgentConfig(
        profile=profile,
        alerts_channel=alerts,
        comms_channel=comms,
        # Where past resolutions are READ from. Normally the same channel we
        # post to, but they diverge deliberately: a test run can still ground
        # itself in the real #comms-noc if the token can read it.
        history_channel=env.get("HISTORY_CHANNEL") or comms,
        post_mode=post_mode,
        trigger_on=trigger_on,
        poll_seconds=int(env.get("POLL_SECONDS") or "60"),
        lookback_minutes=int(env.get("LOOKBACK_MINUTES") or "15"),
        dedupe_window_minutes=int(env.get("DEDUPE_WINDOW_MINUTES") or "60"),
        evidence_dir=Path(env.get("EVIDENCE_DIR") or (_ROOT / "dist" / "evidence")),
        state_dir=Path(env.get("STATE_DIR") or (_ROOT / ".state")),
        slack_bot_token=env.get("SLACK_BOT_TOKEN") or None,
        slack_app_token=env.get("SLACK_APP_TOKEN") or None,
        slack_user_token=env.get("SLACK_USER_TOKEN") or None,
        investigation=(env.get("INVESTIGATION") or "auto").lower(),
        suppressed=_load_suppression(_ROOT / "config" / "suppression.json"),
    )


def describe(config: AgentConfig) -> str:
    return (
        f"PROFILE={config.profile.upper()} "
        f"alerts={config.alerts_channel} comms={config.comms_channel} "
        f"history={config.history_channel} mode={config.post_mode} "
        f"trigger_on={config.trigger_on} "
        f"transport={'socket' if config.socket_mode else f'poll/{config.poll_seconds}s'} "
        f"read_as={'user' if config.slack_user_token else 'bot'}"
        + (" single-channel" if config.single_channel else "")
    )
