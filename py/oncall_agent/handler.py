import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from . import (
    evidence_panels,
    investigate as investigate_mod,
    llm,
    slack_blocks,
    triage as triage_mod,
)
from .Agents import registry as specialists
from .Agents.Edgeui_Agent.engine_failure_pipeline import run_engine_failure_pipeline
from .Agents.Grafana_Agent import grafana
from .Agents.MASTER_Agent import router
from .alert_parser import (
    to_victorops_incident,
    window_minutes_from,
)
from .case_library import CaseLibrary
from .config import AgentConfig
from .slack_history import (
    HistoryFetchResult,
    fetch_channel_context,
    fetch_relevant_comms_noc_history,
)
from .slack_post import DryRunSlackPoster, compose_top_level_text, post_decided_thread
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

# Every specialist's own case library, merged, for the two things that can't
# know in advance which single agent's data applies: fingerprinting an alert
# before routing has even picked a specialist, and the generalist fallback
# for an alert type no specialist claims. A specialist's OWN investigation
# uses only its own case_library (see _handle_specialist below) — never this
# merged view.
CASES = CaseLibrary([case for cfg in specialists.SPECIALISTS.values()
                     for case in cfg["case_library"].cases])


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


def should_handle(alert: ParsedAlert, config: AgentConfig, seen: SeenStore,
                  own_ids: Optional[set] = None) -> tuple:
    """(handle?, reason). Order matters: cheapest, most-certain rejections first."""
    # FIRST, before anything: never react to our own output. Bolt's self-event
    # filter is disabled here because real alerts arrive as bot messages, so
    # this is the only thing standing between the agent and an infinite loop
    # of triaging its own threads — and in single-channel mode that loop is
    # one post away.
    if own_ids and (alert.author_id in own_ids):
        return False, f"posted by this agent ({alert.author_id}) — never triage our own output"
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
    own_ids: Optional[set] = None,
) -> HandledAlert:
    handle, reason = should_handle(alert, config, seen, own_ids=own_ids)
    if not handle:
        log(f"skip #{alert.incident_number or '-'} {alert.incident_name[:70]!r}: {reason}")
        return HandledAlert(alert=alert, route="skipped", reason=reason)

    seen.record(alert.fingerprint)
    # One channel for both: the alert message IS the thread root, so everything
    # the agent has to say hangs off it instead of starting a second thread.
    if config.single_channel and not alert.reply_in_thread_ts:
        alert.reply_in_thread_ts = alert.message_ts
    if not alert.permalink:
        alert.permalink = permalink_for(client, alert.channel_id or config.alerts_channel,
                                        alert.message_ts)

    # Domain-specialist routing (2026-08-26): a deterministic router picks
    # the specialist by alert type — Edge UI/engine, Kubernetes, Grafana/
    # metrics, Runscope — each with its own focused tool surface and expert
    # system prompt, instead of one generalist juggling every domain's tools
    # at once. is_engine_failure_rate alerts are covered by the router too
    # (it checks that first); the old deterministic Playwright pipeline
    # (_handle_engine_failure/run_engine_failure_pipeline, still defined
    # below) is kept but no longer called, as a rollback path until the new
    # Edge UI specialist has proven out live. No routing match falls through
    # to the generalist (_handle_generic_triage) unchanged.
    specialist = router.route_alert(alert)
    if specialist:
        return _handle_specialist(alert, config, client, log, specialist)
    return _handle_generic_triage(alert, config, client, log)


# ------------------------------------------------- investigation by specialist

def _handle_specialist(alert, config, client, log, specialist: str) -> HandledAlert:
    """Dispatch to one domain specialist (Agents/<Name>/, assembled by
    Agents/registry.py) — Claude reads #comms-noc AND that domain's own
    tools itself, then we post what it drafted.

    Evidence hints are still chosen here, not by the model: a mapped Grafana
    panel is offered by key as a starting point (config/panel_map.json is
    only a hint for dashboards someone has already pinned), same as before
    this was split by domain.
    """
    fingerprint = alert.alert_name or CASES.fingerprint(alert.raw_text)
    specs = _panel_specs(alert, fingerprint, log)
    hint = ""
    if specs:
        hint = ("A previously-confirmed dashboard for this alert type: "
                + "; ".join(f"{s.dashboard_uid} panel {s.panel_id} vars={s.variables}"
                            for s in specs)
                + ". Verify it still fits this alert before using it.")

    # This specialist's OWN case library, not the merged CASES above — an
    # edge_ui alert should never get grounded in a Kubernetes case just
    # because both happen to share a fingerprint collision.
    own_cases = specialists.SPECIALISTS[specialist]["case_library"]
    cases = own_cases.find(fingerprint)
    log(f"{specialist} specialist: fingerprint={fingerprint!r} cases={len(cases)} "
        f"{'panel hint available' if specs else 'no panel hint'}")
    try:
        result = specialists.run_specialist(
            alert, specialist, past_cases=cases, panel_hint=hint, fingerprint=fingerprint)
    except Exception as e:                          # noqa: BLE001 — one alert, not the daemon
        log(f"{specialist} investigation failed: {type(e).__name__}: {e}")
        # If the alert is already visible in the channel, silence reads as a
        # hung agent. Say plainly that the investigation failed so a human
        # picks the alert up instead of waiting on us.
        _post_failure_notice(config, client, alert, e, log)
        return HandledAlert(alert=alert, route=specialist,
                            reason=f"failed: {type(e).__name__}: {e}")

    log(f"{specialist}: used {len(result.tools_used)} tool call(s), produced "
        f"{len(result.evidence_files)} evidence file(s), grounded in "
        f"{len(result.prior_incidents)} past thread(s), cost ${result.cost_usd:.3f}")
    _write_audit(config, alert, result, log)

    evidence_files = result.evidence_files
    decision = result.decision
    if not (config.is_live and client):
        DryRunSlackPoster(log=log).post_decided_thread(
            to_victorops_incident(alert), decision, evidence_files)
        return HandledAlert(alert=alert, route=specialist)

    post_decided_thread(client, config.comms_channel, to_victorops_incident(alert),
                        decision, evidence_files, log=log,
                        thread_ts=alert.reply_in_thread_ts,
                        with_buttons=config.socket_mode)
    return HandledAlert(alert=alert, route=specialist,
                        thread_ts=alert.reply_in_thread_ts)


def _write_audit(config, alert, result, log) -> None:
    """Append-only record of what was investigated and decided (DESIGN.md §4.9).

    Written even when should_post is false — "the agent looked and chose not
    to post" is exactly the thing a shadow-mode review needs to see.
    """
    try:
        audit_dir = config.state_dir / "audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        record = result.to_json()
        record["alert"] = {
            "incident_number": alert.incident_number,
            "incident_name": alert.incident_name,
            "incident_url": alert.incident_url,
            "fingerprint": alert.fingerprint,
            "message_ts": alert.message_ts,
        }
        path = audit_dir / f"{alert.incident_number or alert.message_ts}.json"
        path.write_text(json.dumps(record, indent=2))
        log(f"audit written to {path}")
    except OSError as e:
        log(f"could not write audit record: {e}")


# ------------------------------------------------------------ engine failure

def _handle_engine_failure(alert, config, client, log) -> HandledAlert:
    incident = to_victorops_incident(alert)
    window = window_minutes_from(alert)
    log(f"engine-failure route: incident #{incident.incident_number} "
        f"env={alert.environment_key or '?'} window={window}m")

    history = _comms_history(client, config, ["engine failure"], log)
    # Same rules as every other route: reply inside the alert's own thread
    # (mandatory in single-channel mode, or the agent opens a second thread for
    # an alert already visible above it), and keep screenshots in a per-run
    # directory so concurrent investigations cannot overwrite each other's.
    run_dir = config.evidence_dir / "runs" / f"{alert.incident_number or 'engine'}-{os.getpid()}"
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        run_engine_failure_pipeline(
            incident,
            window_minutes=window,
            client=client if config.is_live else None,
            channel=config.comms_channel if config.is_live else None,
            screenshot_dir=run_dir,
            history=history,
            panel_fingerprint=alert.alert_name or CASES.fingerprint(alert.raw_text),
            log=log,
            thread_ts=alert.reply_in_thread_ts,
            with_buttons=config.socket_mode,
        )
    except (Exception, SystemExit) as e:             # noqa: BLE001 — one alert, not the run
        # The pipeline aborts loudly by design (window drift, no failing
        # engine, a guardrail refusing the decision). That must not escape as
        # a traceback: the alert is already in the channel, so silence there
        # reads as a hung agent. SystemExit is included because the pipeline
        # uses it for its abort conditions.
        log(f"engine-failure pipeline failed: {type(e).__name__}: {e}")
        _post_failure_notice(config, client, alert, e, log)
        return HandledAlert(alert=alert, route="engine_failure",
                            reason=f"failed: {type(e).__name__}: {e}",
                            thread_ts=alert.reply_in_thread_ts)
    return HandledAlert(alert=alert, route="engine_failure",
                        thread_ts=alert.reply_in_thread_ts)


def _post_failure_notice(config, client, alert, error, log) -> None:
    """Say in the thread that the investigation failed, rather than going quiet."""
    if not (config.is_live and client and alert.reply_in_thread_ts):
        return
    try:
        client.chat_postMessage(
            channel=config.comms_channel, thread_ts=alert.reply_in_thread_ts,
            text=f":warning: Automated investigation stopped — "
                 f"`{type(error).__name__}: {str(error)[:250]}`\n"
                 f"No evidence was posted. This alert needs a human.")
        log("posted a failure notice in the thread")
    except SlackApiError as post_error:
        log(f"could not post the failure notice: {post_error}")


# ------------------------------------------------------------ generic triage

def _handle_generic_triage(alert, config, client, log) -> HandledAlert:
    """Every alert type without a deterministic evidence pipeline.

    Produces the SAME terse threaded output as the engine-failure route —
    short replies, one observation each, escalation last — rather than the
    7-section report this used to emit. That report came from the noc-ai-lab
    prototype and read nothing like #comms-noc, where the convention is
    "100% CPU utilization on VM" / "Can't login" / "*Restarting* the VM".
    """
    fingerprint = CASES.fingerprint(alert.raw_text) or alert.alert_name
    past_cases = CASES.find(fingerprint)
    history = _comms_history(client, config, CASES.keywords_for(fingerprint), log)
    context = _alerts_context(client, config, log)
    specs = _panel_specs(alert, fingerprint, log)
    rendered = evidence_panels.render_panels(specs, config.evidence_dir, log=log) if specs else []
    evidence_files = {f"grafana_panel_{r.spec.panel_id}": r.path for r in rendered}
    descriptions = {f"grafana_panel_{r.spec.panel_id}": evidence_panels.panel_caption(r.spec)
                    for r in rendered}

    log(f"triage route: fingerprint={fingerprint!r} cases={len(past_cases)} "
        f"threads={len(history.threads)} panels={len(rendered)}/{len(specs)}")

    try:
        result = investigate_mod.draft_from_context(
            alert, past_cases=past_cases, history=history,
            correlation=_context_observation(context),
            evidence_keys=list(evidence_files), evidence_descriptions=descriptions,
        )
    except Exception as e:                          # noqa: BLE001 — one alert, not the daemon
        log(f"triage failed: {type(e).__name__}: {e}")
        return HandledAlert(alert=alert, route="triage", reason=f"failed: {type(e).__name__}: {e}")

    _write_audit(config, alert, result, log)
    incident = to_victorops_incident(alert)
    if not (config.is_live and client):
        DryRunSlackPoster(log=log).post_decided_thread(incident, result.decision, evidence_files)
        return HandledAlert(alert=alert, route="triage")

    post_decided_thread(client, config.comms_channel, incident, result.decision,
                        evidence_files, log=log, thread_ts=alert.reply_in_thread_ts,
                        with_buttons=config.socket_mode)
    return HandledAlert(alert=alert, route="triage", thread_ts=alert.reply_in_thread_ts)


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
            specs = panel_map.specs_for(key, alert.environment_key,
                                        labels=alert.labels, hints=alert.label_hints)
        except Exception as e:                      # noqa: BLE001
            log(f"panel map lookup failed for {key!r}: {e}")
            continue
        if specs:
            return specs
    return []
