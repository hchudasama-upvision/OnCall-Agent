import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from . import evidence_panels, grafana, llm, slack_blocks, triage as triage_mod
from .alert_parser import (
    is_engine_failure_rate,
    to_victorops_incident,
    window_minutes_from,
)
from .case_library import load_case_library
from .config import AgentConfig
from .engine_failure_pipeline import run_engine_failure_pipeline
from .slack_history import (
    HistoryFetchResult,
    fetch_channel_context,
    fetch_relevant_comms_noc_history,
)
from .slack_post import compose_top_level_text
from .types import ParsedAlert

"""
The dispatcher: one ParsedAlert in, one investigated #comms-noc thread out.

Two routes, chosen deterministically:

  engine-failure-rate  -> the existing Edge UI pipeline. Real task stats,
                          real Edge UI screenshots, real downloaded logs,
                          then decide_resolution.py writes the thread.
                          Unchanged from what scripts/run_live_test.py has
                          always done.
  everything else      -> generic triage. Case library + real recent
                          #comms-noc threads + #alerts-devops correlation
                          context + mapped Grafana panels, then the
                          7-section report.

The route is picked by code, not by the model, and so is every piece of
evidence attached. The model writes narrative and routing recommendations.
"""

CASES = load_case_library()


@dataclass
class HandledAlert:
    alert: ParsedAlert
    route: str                 # engine_failure | triage | skipped
    reason: str = ""
    thread_ts: str = ""


class SeenStore:
    """Fingerprint -> last-handled epoch, persisted so a restart does not
    re-investigate everything still inside the dedupe window.

    Dedup is a prerequisite, not a nicety: DESIGN.md §1.5 item 2 records the
    same real-world condition arriving two or three times (local + central
    Alertmanager, plus the VictorOps mirror), and repeat FIRING:N re-posts
    every 5 minutes on top of that.
    """

    def __init__(self, path: Path, window_minutes: int):
        self.path = path
        self.window_seconds = window_minutes * 60
        self.seen = {}
        if path.exists():
            try:
                self.seen = json.loads(path.read_text())
            except (ValueError, OSError):
                self.seen = {}

    def is_duplicate(self, fingerprint: str, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        last = self.seen.get(fingerprint)
        return bool(last and now - last < self.window_seconds)

    def record(self, fingerprint: str, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        self.seen[fingerprint] = now
        cutoff = now - self.window_seconds
        self.seen = {k: v for k, v in self.seen.items() if v >= cutoff}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.seen))


def permalink_for(client: Optional[WebClient], channel: str, ts: str) -> str:
    """The alert's own Slack permalink, or "" — never fabricated.

    decide_resolution refuses to post any URL that is not this one, so an
    empty string here just means the thread refers to the incident as plain
    text.
    """
    if not client or not ts:
        return ""
    try:
        return client.chat_getPermalink(channel=channel, message_ts=ts).get("permalink", "")
    except SlackApiError:
        return ""


def should_handle(alert: ParsedAlert, config: AgentConfig, seen: SeenStore) -> tuple:
    """(handle?, reason). Order matters: cheapest, most-certain rejections first."""
    if config.trigger_on == "victorops" and not alert.is_trigger:
        return False, f"not a trigger ({alert.source}/{alert.kind}); kept as correlation context only"
    if alert.kind in ("rotation", "incident_update"):
        return False, f"{alert.kind} — state change, not a new investigation"
    if config.is_suppressed(alert.fingerprint):
        return False, f"suppressed fingerprint ({alert.fingerprint})"
    if seen.is_duplicate(alert.fingerprint):
        return False, f"duplicate within {config.dedupe_window_minutes}m ({alert.fingerprint})"
    return True, ""


def handle_alert(
    alert: ParsedAlert,
    config: AgentConfig,
    client: Optional[WebClient],
    seen: SeenStore,
    log: Callable[[str], None] = print,
) -> HandledAlert:
    handle, reason = should_handle(alert, config, seen)
    if not handle:
        log(f"skip #{alert.incident_number or '-'} {alert.incident_name[:70]!r}: {reason}")
        return HandledAlert(alert=alert, route="skipped", reason=reason)

    seen.record(alert.fingerprint)
    if not alert.permalink:
        alert.permalink = permalink_for(client, alert.channel_id or config.alerts_channel,
                                        alert.message_ts)

    if is_engine_failure_rate(alert):
        return _handle_engine_failure(alert, config, client, log)
    return _handle_generic_triage(alert, config, client, log)


# ------------------------------------------------------------ engine failure

def _handle_engine_failure(alert, config, client, log) -> HandledAlert:
    incident = to_victorops_incident(alert)
    window = window_minutes_from(alert)
    log(f"engine-failure route: incident #{incident.incident_number} "
        f"env={alert.environment_key or '?'} window={window}m")

    history = _comms_history(client, config, ["engine failure"], log)
    run_engine_failure_pipeline(
        incident,
        window_minutes=window,
        client=client if config.is_live else None,
        channel=config.comms_channel if config.is_live else None,
        screenshot_dir=config.evidence_dir,
        history=history,
        panel_fingerprint=alert.alert_name or CASES.fingerprint(alert.raw_text),
        log=log,
    )
    return HandledAlert(alert=alert, route="engine_failure")


# ------------------------------------------------------------ generic triage

def _handle_generic_triage(alert, config, client, log) -> HandledAlert:
    fingerprint = CASES.fingerprint(alert.raw_text) or alert.alert_name
    past_cases = CASES.find(fingerprint)
    history = _comms_history(client, config, CASES.keywords_for(fingerprint), log)
    context = _alerts_context(client, config, log)
    specs = _panel_specs(alert, fingerprint, log)

    log(f"triage route: fingerprint={fingerprint!r} cases={len(past_cases)} "
        f"threads={len(history.threads)} panels={len(specs)}")

    if not (config.is_live and client):
        return _dry_run_triage(alert, fingerprint, past_cases, history, context, specs, log)

    parent = client.chat_postMessage(
        channel=config.comms_channel,
        text=compose_top_level_text(to_victorops_incident(alert)) if alert.incident_number
        else f"*Alert:*\n> {alert.incident_name or fingerprint}",
    )
    thread_ts = parent["ts"]

    # Placeholder BEFORE the render thread starts. chat_update keeps the
    # original ts, so the triage stays above the panels that land while it
    # runs; posting it fresh at the end would bury it under them. And a
    # multi-second model call with nothing in the thread reads as a hung bot.
    placeholder = client.chat_postMessage(
        channel=config.comms_channel, thread_ts=thread_ts,
        text=(f":hourglass_flowing_sand: Investigating with {llm.model_label()} — "
              f"{len(past_cases)} past case(s), {len(history.threads)} #comms-noc thread(s), "
              f"{len(specs)} evidence panel(s)…"),
    )

    # Renders run alongside the model call rather than after it: ~4-7s per
    # panel against a real Grafana would otherwise be pure added latency.
    if specs:
        threading.Thread(
            target=evidence_panels.post_panels,
            args=(client, config.comms_channel, thread_ts, specs, config.evidence_dir, log),
            daemon=True,
        ).start()

    try:
        analysis = triage_mod.triage(
            triage_mod.alert_prompt_text(alert), past_cases, history,
            observations=_context_observation(context),
        )
        blocks = slack_blocks.triage_blocks(analysis, fingerprint,
                                            with_buttons=config.socket_mode)
    except Exception as e:                          # noqa: BLE001 — must reach the thread
        log(f"triage failed: {e}")
        analysis, blocks = f":warning: Triage failed: {e}", None

    update = dict(channel=config.comms_channel, ts=placeholder["ts"],
                  text=slack_blocks.fallback_text(analysis))
    if blocks:                    # a failure message stays plain text, no buttons
        update["blocks"] = blocks
    client.chat_update(**update)
    return HandledAlert(alert=alert, route="triage", thread_ts=thread_ts)


def _dry_run_triage(alert, fingerprint, past_cases, history, context, specs, log) -> HandledAlert:
    log(f"\n[DRY RUN] would post top-level:\n"
        f"*Alert:*\n> {alert.incident_name or fingerprint}")
    for spec in specs:
        log(f"[DRY RUN] would attach {evidence_panels.panel_caption(spec)}")
    try:
        analysis = triage_mod.triage(
            triage_mod.alert_prompt_text(alert), past_cases, history,
            observations=_context_observation(context),
        )
    except Exception as e:                          # noqa: BLE001
        log(f"[DRY RUN] triage failed: {e}")
        return HandledAlert(alert=alert, route="triage", reason=f"triage failed: {e}")
    log(f"\n[DRY RUN] would reply in thread:\n{analysis}\n")
    return HandledAlert(alert=alert, route="triage")


# ------------------------------------------------------------------ evidence

def _comms_history(client, config, keywords, log) -> HistoryFetchResult:
    """Past #comms-noc resolutions for this alert type.

    Needs channels:history (public) or groups:history (private) on the bot
    token, plus the bot invited to #comms-noc if it is private. Both are
    Slack-admin actions; without them this returns an empty result with the
    reason attached, and triage proceeds on the offline case library alone
    rather than crashing.
    """
    if not client:
        return HistoryFetchResult(threads=[], unavailable_reason="no Slack client configured (dry run)")
    result = fetch_relevant_comms_noc_history(client, keywords, channel_id=config.history_channel)
    if result.unavailable_reason:
        log(f"#comms-noc history unavailable: {result.unavailable_reason}")
    return result


def _alerts_context(client, config, log) -> HistoryFetchResult:
    """What else was firing in #alerts-devops around this alert."""
    if not client:
        return HistoryFetchResult(threads=[], unavailable_reason="no Slack client configured (dry run)")
    result = fetch_channel_context(client, config.alerts_channel, limit=40)
    if result.unavailable_reason:
        log(f"#alerts-devops context unavailable: {result.unavailable_reason}")
    return result


def _context_observation(context: HistoryFetchResult) -> Optional[str]:
    """Correlation context as the OBSERVATIONS prompt input.

    Labelled as what it is — recent channel traffic, not a diagnosis — so the
    model can spot a shared-path failure without treating co-occurrence as
    causation.
    """
    if context.unavailable_reason or not context.threads:
        return None
    lines = "\n".join(f"  - {t.top_level_text.splitlines()[0][:200]}" for t in context.threads[:25])
    return ("Recent #alerts-devops traffic around this alert (correlation context only — "
            f"co-occurrence is not causation, and duplicates across Alertmanager paths are "
            f"expected):\n{lines}")


def _panel_specs(alert: ParsedAlert, fingerprint: str, log) -> List[evidence_panels.PanelSpec]:
    """Mapped panels for this alert, trying the most specific key first.

    The alert's own alertname ("Engine failure rate above 15%") is the
    precise key; the case-library fingerprint is the fallback, and for an
    alert type with no library entry that fingerprint is a regex slice of the
    Slack line, which is exactly why it is second and not first.
    """
    if not grafana.is_configured():
        return []
    try:
        panel_map = evidence_panels.load_panel_map()
    except Exception as e:                          # noqa: BLE001 — config error, not fatal
        log(f"panel_map.json could not be loaded: {e}")
        return []
    for key in [k for k in (alert.alert_name, fingerprint, alert.incident_name) if k]:
        try:
            specs = panel_map.specs_for(key, alert.environment_key)
        except Exception as e:                      # noqa: BLE001
            log(f"panel map lookup failed for {key!r}: {e}")
            continue
        if specs:
            return specs
    return []
