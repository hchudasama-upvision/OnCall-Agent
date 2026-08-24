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
# "alert-devops"; the actual names are #alerts-devops and #comms-noc, and
# both are overridable by name or by id in .env.
DEFAULT_ALERTS_CHANNEL = "C909ZH4ET"    # #alerts-devops
DEFAULT_COMMS_CHANNEL = "C01F810QM96"   # #comms-noc


@dataclass
class AgentConfig:
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
    suppressed: List[re.Pattern] = field(default_factory=list)

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

    # SLACK_CHANNEL is this repo's existing name for "where the agent posts",
    # kept as the fallback so an untouched .env keeps working.
    comms = env.get("COMMS_CHANNEL") or env.get("SLACK_CHANNEL") or DEFAULT_COMMS_CHANNEL

    return AgentConfig(
        alerts_channel=env.get("ALERTS_CHANNEL") or DEFAULT_ALERTS_CHANNEL,
        comms_channel=comms,
        # Where past resolutions are READ from. Normally the same channel we
        # post to, but they diverge when POST_MODE is pointed at a test
        # channel while history still comes from the real #comms-noc.
        history_channel=env.get("HISTORY_CHANNEL") or DEFAULT_COMMS_CHANNEL,
        post_mode=post_mode,
        trigger_on=trigger_on,
        poll_seconds=int(env.get("POLL_SECONDS") or "60"),
        lookback_minutes=int(env.get("LOOKBACK_MINUTES") or "15"),
        dedupe_window_minutes=int(env.get("DEDUPE_WINDOW_MINUTES") or "60"),
        evidence_dir=Path(env.get("EVIDENCE_DIR") or (_ROOT / "dist" / "evidence")),
        state_dir=Path(env.get("STATE_DIR") or (_ROOT / ".state")),
        slack_bot_token=env.get("SLACK_BOT_TOKEN") or None,
        slack_app_token=env.get("SLACK_APP_TOKEN") or None,
        suppressed=_load_suppression(_ROOT / "config" / "suppression.json"),
    )


def describe(config: AgentConfig) -> str:
    return (
        f"alerts={config.alerts_channel} comms={config.comms_channel} "
        f"history={config.history_channel} mode={config.post_mode} "
        f"trigger_on={config.trigger_on} "
        f"transport={'socket' if config.socket_mode else f'poll/{config.poll_seconds}s'}"
    )
