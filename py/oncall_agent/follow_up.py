#!/usr/bin/env python3
"""
The second look: re-check an alert a few minutes later and say in the SAME
thread whether it recovered.

Why this exists
---------------
The real runbook for API response-code alerts (DESIGN.md §2) starts with "wait
5-10 minutes for self-heal", and most of these alerts do exactly that. Until
now the agent posted once, at the moment of firing, and the thread never said
whether the thing came back — a human had to go look at the dashboard anyway,
which is the work this is supposed to remove. The owner's direction
(2026-08-28): re-check after 5-10 minutes, and if the success rate is at or
above 99.99%, post that with a fresh screenshot of the same dashboard, in the
same thread.

What is code's decision and what is not
---------------------------------------
All of it is code's. The threshold comparison, the timing, the wording and
whether anything is posted at all are deterministic here (CLAUDE.md
non-negotiable #1) — there is no model call in this file. The number comes from
Grafana_Agent/api_health.py, which replays the dashboard panel's own queries,
so the figure in the thread is the figure on the dashboard. A model that
"remembers" a rate cannot influence this, and a rate that cannot be read posts
nothing rather than a guess.

Restart behaviour
-----------------
A pending re-check is persisted to .state/followups/ before its timer starts,
so a listener restart during the wait resumes it instead of silently dropping
it (the alternative — an in-memory timer only — loses the second post exactly
when a deploy is happening, which is when these alerts fire most). A record is
deleted once it is resolved or has run out of attempts. Records older than
FOLLOW_UP_MAX_AGE_MINUTES are dropped on resume rather than posting something
stale into a thread nobody is reading any more.
"""
import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .Agents.AWS_Agent import aws_client, server as aws_server
from .Agents.Grafana_Agent import api_health, grafana, grafana_capture, thanos
from .Agents.Grafana_Agent import server as grafana_server

# When to look again, in minutes after the first post. The runbook says 5-10,
# so that is the default: two attempts, and the sequence ends at the first one
# that can post a verdict.
FOLLOW_UP_MINUTES = [float(m) for m in
                     (os.environ.get("FOLLOW_UP_MINUTES", "5,10").split(",")) if m.strip()]

# Some families want a different cadence. ALB target health is one: the owner
# asked to look again after 2-3 minutes, because a warming-up instance either
# passes its health checks in that window or is genuinely broken, and 5 minutes
# is long enough to miss the transition. FOLLOW_UP_MINUTES stays the default for
# everything else, and an explicit FOLLOW_UP_MINUTES overrides all of it.
_KIND_MINUTES = {"alb_health": "3,6"}


def minutes_for(kind: str) -> List[float]:
    explicit = os.environ.get("FOLLOW_UP_MINUTES")
    if explicit:
        return [float(m) for m in explicit.split(",") if m.strip()]
    override = os.environ.get(f"FOLLOW_UP_MINUTES_{kind.upper()}") or _KIND_MINUTES.get(kind)
    if override:
        return [float(m) for m in str(override).split(",") if str(m).strip()]
    return FOLLOW_UP_MINUTES

# A resumed record older than this is dropped unposted — the incident has moved
# on and a "recovered" line arriving an hour late is noise, not news.
MAX_AGE_MINUTES = float(os.environ.get("FOLLOW_UP_MAX_AGE_MINUTES", "45"))

_STATE_SUBDIR = "followups"

# RDS alert names: RDS_CPUUtilizationAvgCriticalCore, RDS_FreeableMemory, …
_RDS_ALERT = re.compile(r"\bRDS[_ ]|\bAurora\b", re.I)

# Words every RDS instance name and every RDS alert contains, so they
# distinguish nothing — same lesson as the api_health panel pairing.
_GENERIC_DB_WORDS = {"rds", "rds2", "db", "database", "cluster", "instance", "aurora"}

# Alerts whose NAME is a CloudWatch alarm name. Narrow on purpose: the
# throttling family is what the owner asked for, and a loose pattern here
# would arm re-checks on alerts nobody asked to be re-checked.
_CW_ALARM_ALERT = re.compile(r"ThrottledCount|Rekognition|Throttl\w*-High", re.I)

# ALBUnhealthyHostCritical / ...Warning — the load-balancer target-health family.
_ALB_ALERT = re.compile(r"ALBUnhealthyHost|AlbUnhealthyHost|UnhealthyHost", re.I)

# "High concurrent_requests for core-admin-server", "High nodejs_active_handles
# for core-graphql-server" — the family whose rule lives in Thanos.
_PROMQL_RULE_ALERT = re.compile(
    r"High\s+(concurrent_requests|nodejs_active_handles|\w+)\s+for\s+\S+"
    # The vCenter host alerts are Thanos rules too (node.rules), so they get the
    # same treatment: rule lookup for the threshold, duration from the series.
    r"|Host(CPU|Memory)Utilization|HostOutOfMemory", re.I)


@dataclass
class PendingCheck:
    """One scheduled re-check. Serialized as-is into .state/followups/.

    `kind` decides what the re-check DOES, so this stayed one mechanism instead
    of two: "api_health" re-reads the success rate and posts only on a verdict,
    "rds_usage" re-renders the usage graphs and always posts them (the owner
    asked for the graph again after 5-10 minutes, not for a threshold).
    """
    incident_number: str
    alert_name: str
    thread_ts: str
    channel: str
    codes_panel: int
    rate_panel: int
    environment: str
    created_at: float
    due_at: float
    kind: str = "api_health"
    target: str = ""            # rds_usage: DB instance; cloudwatch/thanos: alarm or rule name
    # Resolved evidence coordinates, filled at schedule time so the re-check does
    # not have to re-derive them from an alert it no longer has. For thanos_usage
    # this carries the mapped Grafana panel (dashboard uid, panel id, variables),
    # because the ESXi alerts DO have a dashboard while most rule alerts do not.
    params: Dict[str, str] = field(default_factory=dict)
    attempt: int = 1
    attempts_total: int = field(default_factory=lambda: len(FOLLOW_UP_MINUTES) or 1)

    @property
    def key(self) -> str:
        return f"{self.incident_number or self.thread_ts}-{self.attempt}"

    @property
    def is_final(self) -> bool:
        return self.attempt >= self.attempts_total


# --------------------------------------------------------------- eligibility

def kind_for(alert) -> str:
    """Which re-check this alert gets, or "" for none.

    Two families qualify so far, each for a stated reason rather than a wish for
    a second post: API response codes (the runbook says wait 5-10 min for
    self-heal, and one number decides recovered) and RDS (the owner asked for
    the usage graph again after 5-10 min, because a CPU spike that has passed
    and one that is still climbing need different human responses).
    """
    name = f"{alert.alert_name or ''} {alert.raw_text or ''}"
    if _RDS_ALERT.search(name):
        return "rds_usage"
    # A CloudWatch-alarm-shaped alert (Rekognition-ThrottledCount-High-wpsc01):
    # the owner asked for repeated captures, so this kind posts at EVERY attempt
    # rather than once.
    if _ALB_ALERT.search(name):
        return "alb_health"
    if _CW_ALARM_ALERT.search(name):
        return "cloudwatch_usage"
    # PromQL-rule alerts: the title is the rule name, so the threshold and the
    # duration are lookups. The owner asked for periodic screenshots on these.
    if _PROMQL_RULE_ALERT.search(name):
        return "thanos_usage"
    if grafana.is_configured() and api_health.is_api_health_alert(alert.alert_name or "",
                                                                 alert.raw_text or ""):
        return "api_health"
    return ""


def applies(alert) -> bool:
    return bool(kind_for(alert))


# ---------------------------------------------------------------- scheduling

def _state_dir(config) -> Path:
    path = Path(config.state_dir) / _STATE_SUBDIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _persist(config, check: PendingCheck) -> Path:
    path = _state_dir(config) / f"{check.key}.json"
    path.write_text(json.dumps(asdict(check), indent=2))
    return path


def _forget(config, check: PendingCheck) -> None:
    path = _state_dir(config) / f"{check.key}.json"
    if path.exists():
        path.unlink()


def schedule(alert, config, client, log: Callable[[str], None] = print,
             thread_ts: str = "", attempt: int = 1) -> Optional[PendingCheck]:
    """Arm the next re-check for this alert, or return None if there is nothing
    to arm (not an eligible alert type, no thread to reply into, no panel)."""
    thread_ts = thread_ts or alert.reply_in_thread_ts
    kind = kind_for(alert)
    schedule = minutes_for(kind)
    if not kind or attempt > len(schedule):
        return None
    if not thread_ts:
        log("follow-up not scheduled: no thread to reply into")
        return None

    codes_panel = rate_panel = 0
    environment = target = ""
    params: Dict[str, str] = {}
    if kind == "api_health":
        try:
            pair = api_health.find_panel_pair(alert.alert_name or alert.incident_name)
        except Exception as e:                          # noqa: BLE001 — never kill the alert
            log(f"follow-up not scheduled: could not resolve the dashboard panel ({e})")
            return None
        if not pair or not pair.rate_panel:
            log(f"follow-up not scheduled: no confident success-rate panel for "
                f"{alert.alert_name!r} — refusing to quote another environment's number")
            return None
        codes_panel, rate_panel, environment = pair.codes_panel, pair.rate_panel, pair.environment
    elif kind == "alb_health":
        target = _load_balancer_for(alert, log)
        if not target:
            return None
        environment = target
    elif kind == "cloudwatch_usage":
        target = _alarm_name_for(alert, log)
        if not target:
            return None
        environment = target
    elif kind == "thanos_usage":
        # Confirm the rule exists before arming: without it there is no
        # threshold, and a re-check with no threshold cannot say high or low.
        try:
            rules = thanos.find_alert_rules(alert.alert_name or alert.incident_name)
        except Exception as e:                          # noqa: BLE001
            log(f"follow-up not scheduled: Thanos unreachable ({e})")
            return None
        if not rules:
            log(f"follow-up not scheduled: no Thanos alerting rule matches "
                f"{alert.alert_name!r}")
            return None
        target = rules[0].name
        environment = target
        # If this alert type has a pinned dashboard panel (ESXi host CPU/memory
        # do), remember it now: the owner asked for the CPU usage panel captured
        # at different times, and re-deriving it later would need the alert's
        # labels, which the persisted record does not keep.
        params = _panel_params(alert, log)
        scope = _scope_selector(params)
        if scope:
            params["scope"] = scope
    else:
        target = _rds_instance_for(alert, log)
        if not target:
            return None
        environment = target

    delay_minutes = schedule[attempt - 1]
    check = PendingCheck(
        incident_number=str(alert.incident_number or ""),
        alert_name=alert.alert_name or alert.incident_name,
        thread_ts=thread_ts,
        channel=config.comms_channel,
        codes_panel=codes_panel,
        rate_panel=rate_panel,
        environment=environment,
        created_at=time.time(),
        due_at=time.time() + delay_minutes * 60,
        kind=kind,
        target=target,
        params=params,
        attempt=attempt,
        attempts_total=len(schedule),
    )
    _persist(config, check)
    detail = {"api_health": f"panel {check.rate_panel}, threshold "
                            f"{api_health.RECOVERY_THRESHOLD * 100:.4f}%",
              "rds_usage": f"RDS {check.target}, usage graphs",
              "cloudwatch_usage": f"alarm {check.target}, metric graph each attempt",
              "thanos_usage": f"rule {check.target}, Thanos graph each attempt",
              "alb_health": f"load balancer {check.target}, target health each attempt",
              }.get(kind, kind)
    log(f"follow-up {check.attempt}/{check.attempts_total} armed for "
        f"{check.alert_name!r} [{kind}]: re-check in {delay_minutes:g}m ({detail})")
    _arm_timer(check, config, client, log)
    return check


def _rds_instance_for(alert, log: Callable[[str], None]) -> str:
    """Which database this alert is about.

    An RDS alert names the environment and a role ("us-1 : prod -
    RDS_CPUUtilizationAvgCriticalCore" -> the prod CORE database), not the
    instance id. Resolution is by matching the alert text against the instance
    identifiers that actually exist in the configured accounts — never by
    constructing a name, because a constructed name that happens to exist in
    another account would put the wrong database's graph in the thread.
    """
    raw = f"{alert.alert_name or ''} {alert.incident_name or ''} {alert.raw_text or ''}"
    override = os.environ.get("FOLLOW_UP_RDS_INSTANCE", "").strip()
    if override:
        return override

    # The card NAMES the database. Since 2026-09-11 the Alertmanager cards
    # carry a state_message block whose labels are spelled out, and RDS alerts
    # put the instance in `dbinstance_identifier` — verified on incident
    # #121453, "dbinstance_identifier: stage-media-rds2". A named label is not
    # a guess, so it wins outright and the word-scoring below never runs.
    # It also needs no AWS call, so the re-check can still be armed when the
    # operator's SSO session has expired at arm time — which is exactly when
    # the old path returned "" and silently scheduled nothing.
    named = (alert.label_hints or {}).get("dbinstance_identifier", "").strip()
    if named:
        log(f"RDS instance {named} taken from the alert's own dbinstance_identifier label")
        return named

    try:
        instances = [i.get("DBInstanceIdentifier", "") for i in aws_client.list_db_instances()]
    except Exception as e:                              # noqa: BLE001
        log(f"follow-up not scheduled: could not list RDS instances ({e})")
        return ""

    text = raw.lower()
    for name in instances:                              # an exact identifier wins outright
        if name and name.lower() in text:
            return name

    # Otherwise match on WORDS, not substrings. "RDS_CPUUtilizationAvgCriticalCore"
    # is split on camelCase too, so its trailing "Core" is a word that can match
    # the *-core-rds instances — while plain substring matching found "core"
    # inside "CriticalCore" and "rds" inside "RDS_CPUUtilization", which scored
    # every instance and picked a database the alert never named.
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", raw)
    words = {w for w in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if w}
    scored: dict = {}
    for name in instances:
        parts = [p for p in re.split(r"[-_]", name.lower())
                 if len(p) > 2 and not p.isdigit() and p not in _GENERIC_DB_WORDS]
        score = sum(1 for p in parts if p in words)
        if score:
            scored.setdefault(score, []).append(name)
    if scored:
        best = max(scored)
        if len(scored[best]) == 1:
            return scored[best][0]
        log(f"follow-up not scheduled: {alert.alert_name!r} matches several instances "
            f"({', '.join(scored[best])}) — refusing to guess which database")
        return ""
    log(f"follow-up not scheduled: no RDS instance matches {alert.alert_name!r} — "
        f"the alert names no database and nothing in it distinguishes one")
    return ""


def _arm_timer(check: PendingCheck, config, client, log) -> None:
    delay = max(0.0, check.due_at - time.time())
    timer = threading.Timer(delay, _run, args=(check, config, client, log))
    timer.daemon = True        # never hold the listener open on shutdown
    timer.name = f"followup-{check.key}"
    timer.start()


def resume_pending(config, client, log: Callable[[str], None] = print) -> List[PendingCheck]:
    """Re-arm everything that was still waiting when the process stopped."""
    resumed: List[PendingCheck] = []
    directory = _state_dir(config)
    for path in sorted(directory.glob("*.json")):
        try:
            check = PendingCheck(**json.loads(path.read_text()))
        except (ValueError, TypeError) as e:
            log(f"dropping unreadable follow-up {path.name}: {e}")
            path.unlink()
            continue
        age_minutes = (time.time() - check.created_at) / 60
        if age_minutes > MAX_AGE_MINUTES:
            log(f"dropping stale follow-up {path.name} ({age_minutes:.0f}m old)")
            path.unlink()
            continue
        log(f"resuming follow-up {check.key} for {check.alert_name!r} "
            f"(due in {max(0.0, check.due_at - time.time()) / 60:.1f}m)")
        _arm_timer(check, config, client, log)
        resumed.append(check)
    return resumed


# ------------------------------------------------------------------ the check

def compose_message(check: PendingCheck, rate: api_health.SuccessRate) -> str:
    """The follow-up post. A fixed template — no model writes this.

    Field-block shape, matching what the owner asked for on the pod alerts:
    label bold with single asterisks, one field per line, values in backticks.
    """
    verdict = ("recovered — at or above `"
               f"{api_health.RECOVERY_THRESHOLD * 100:.4f}%`" if rate.recovered
               else f"still below `{api_health.RECOVERY_THRESHOLD * 100:.4f}%`")
    counts = ", ".join(f"`{band}` `{count}`" for band, count in sorted(rate.counts.items()))
    schedule = minutes_for(check.kind)
    minutes = schedule[check.attempt - 1] if check.attempt <= len(schedule) else 0
    return "\n".join([
        f"*Re-check (+{minutes:g}m):* {verdict}",
        f"*Success rate:* `{rate.percent}`",
        f"*Window:* `{rate.window}`",
        f"*Requests:* {counts}",
        f"*Panel:* `{check.rate_panel}` on `API Services - Overview`",
    ])


def _render(check: PendingCheck, config, log) -> List[Path]:
    """Fresh screenshots of BOTH panels — the response-codes timeseries and the
    success-rate stat (owner's direction, 2026-08-28).

    Two images rather than one is a deliberate exception to EVIDENCE_ECONOMY's
    one-picture rule for this alert family: the stat panel is the number a human
    looks for first, and the timeseries is what says which codes moved. They
    answer different questions, which is exactly the test that rule sets.

    Returns whatever rendered — an empty list still posts the message, since the
    computed number is the point and a missing picture is not worth losing it.
    """
    out_dir = Path(config.evidence_dir) / "followups"
    out_dir.mkdir(parents=True, exist_ok=True)
    wanted = [("rate", check.rate_panel), ("codes", check.codes_panel)]
    rendered: List[Path] = []
    try:
        with grafana_capture.grafana_browser() as context:
            for label, panel_id in wanted:
                if not panel_id:
                    continue
                out_path = out_dir / f"{check.key}-{label}{panel_id}.png"
                try:
                    grafana_capture.capture_panel(context, api_health.DASHBOARD_UID, panel_id,
                                                  out_path, from_="now-1h", to="now")
                    rendered.append(out_path)
                except Exception as e:                  # noqa: BLE001 — one panel, not both
                    log(f"follow-up: panel {panel_id} did not render "
                        f"({type(e).__name__}: {e})")
    except Exception as e:                              # noqa: BLE001
        log(f"follow-up screenshots failed ({type(e).__name__}: {e}) — posting the number alone")
    return rendered


def _run(check: PendingCheck, config, client, log: Callable[[str], None] = print) -> None:
    """Do the re-check. Which check depends on `kind`; both are deterministic."""
    if check.kind == "rds_usage":
        _run_rds(check, config, client, log)
        return
    if check.kind == "cloudwatch_usage":
        _run_cloudwatch(check, config, client, log)
        return
    if check.kind == "thanos_usage":
        _run_thanos(check, config, client, log)
        return
    if check.kind == "alb_health":
        _run_alb(check, config, client, log)
        return
    try:
        rate = api_health.success_rate(check.rate_panel)
    except Exception as e:                              # noqa: BLE001 — one alert, not the daemon
        log(f"follow-up {check.key}: could not read the success rate "
            f"({type(e).__name__}: {e})")
        _forget(config, check)
        if not check.is_final:
            _reschedule(check, config, client, log)
        return

    log(f"follow-up {check.key}: {rate.percent} over {rate.window} "
        f"({'recovered' if rate.recovered else 'still below threshold'})")
    _forget(config, check)

    # Not recovered and there is another look coming: say nothing yet. A
    # "still broken" line every few minutes is noise, and the on-call engineer
    # is already looking at the first post.
    if not rate.recovered and not check.is_final:
        _reschedule(check, config, client, log)
        return

    message = compose_message(check, rate)
    images = _render(check, config, log)
    _post(check, config, client, message, images, log)


def compose_rds_message(check: PendingCheck, numbers: str) -> str:
    """The RDS follow-up post — a fixed template, no model writes this."""
    schedule = minutes_for(check.kind)
    minutes = schedule[check.attempt - 1] if check.attempt <= len(schedule) else 0
    lines = [f"*Re-check (+{minutes:g}m):* usage now", f"*Instance:* `{check.target}`"]
    # rds_metrics returns "  CPUUtilization: now 3.90, peak 28.94 (avg 8.60) Percent"
    for line in numbers.splitlines():
        stripped = line.strip()
        if ":" not in stripped or stripped.endswith(":"):
            continue
        label, _, value = stripped.partition(":")
        if label not in ("CPUUtilization", "DatabaseConnections", "FreeableMemory",
                         "ReadIOPS", "WriteIOPS", "DiskQueueDepth"):
            continue
        # "now 3.90, peak 28.94 (avg 8.60) Percent" -> "now `3.90%`, peak `28.94%`…"
        text = value.strip()
        suffix = ""
        for unit, shown in (("Percent", "%"), ("Count/Second", "/s"), ("Count", ""),
                            ("Seconds", "s")):
            if text.endswith(unit):
                text, suffix = text[: -len(unit)].rstrip(), shown
                break
        pretty = re.sub(r"(\d[\d.]*(?:GB|MB|KB|B)?)", lambda m: f"`{m.group(1)}{suffix}`", text)
        lines.append(f"*{label}:* {pretty}")
    return "\n".join(lines)


def _run_rds(check: PendingCheck, config, client, log: Callable[[str], None]) -> None:
    """Re-render the usage graphs and post them, whatever the numbers say.

    No threshold here on purpose: the owner asked for the usage graph again
    after 5-10 minutes, and "CPU has come back down" and "CPU is still climbing"
    are both worth seeing. So this posts on the FIRST re-check and stops —
    a second identical graph five minutes later adds nothing.
    """
    _forget(config, check)
    try:
        numbers = aws_server.tool_rds_metrics(check.target, hours=1)
    except Exception as e:                              # noqa: BLE001
        log(f"follow-up {check.key}: could not read RDS metrics ({type(e).__name__}: {e})")
        return
    if numbers.startswith("LOOKUP FAILED"):
        log(f"follow-up {check.key}: {numbers.splitlines()[0]}")
        return

    out_dir = Path(config.evidence_dir) / "followups"
    out_dir.mkdir(parents=True, exist_ok=True)
    images: List[Path] = []
    original_dir = aws_server.EVIDENCE_DIR
    aws_server.EVIDENCE_DIR = out_dir
    try:
        for metric in ("CPUUtilization", "DatabaseConnections"):
            key = f"{check.key}-{metric.lower()}"
            answer = aws_server.tool_rds_metric_graph(
                check.target, metrics=[metric], hours=1, key=key,
                title=f"{check.target} — {metric} (re-check +"
                      f"{minutes_for(check.kind)[check.attempt - 1]:g}m)")
            path = out_dir / f"{key}.png"
            if answer.startswith("OK") and path.exists():
                images.append(path)
            else:
                log(f"follow-up {check.key}: {metric} graph not rendered — "
                    f"{answer.splitlines()[0]}")
    finally:
        aws_server.EVIDENCE_DIR = original_dir

    log(f"follow-up {check.key}: RDS usage re-checked, {len(images)} graph(s)")
    _post(check, config, client, compose_rds_message(check, numbers), images, log)


def _alarm_name_for(alert, log: Callable[[str], None]) -> str:
    """The CloudWatch alarm this alert names.

    For this family the alert title IS the alarm name
    ("Rekognition-ThrottledCount-High-wpsc01"), so the token that looks like an
    alarm name is taken and then CONFIRMED against CloudWatch. Confirming
    matters: an unverified name would send the re-check hunting a
    non-existent alarm five minutes later, in a thread nobody is watching.
    """
    override = os.environ.get("FOLLOW_UP_ALARM", "").strip()
    if override:
        return override
    candidates = re.findall(r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+){2,}",
                            f"{alert.alert_name or ''} {alert.incident_name or ''}")
    for name in sorted(set(candidates), key=len, reverse=True):
        try:
            aws_client.find_alarm(name)
            return name
        except Exception:                               # noqa: BLE001 — try the next shape
            continue
    log(f"follow-up not scheduled: no CloudWatch alarm in {alert.alert_name!r} could be "
        f"confirmed in {', '.join(aws_client.profiles())}")
    return ""


def _run_cloudwatch(check: PendingCheck, config, client, log: Callable[[str], None]) -> None:
    """Re-render the alarm's metric and post it — at EVERY attempt.

    The owner asked for multiple captures across 5-10 minutes for the throttling
    alarm, so unlike rds_usage (one post) and api_health (post on a verdict),
    this one posts each time and lets the sequence show the shape of the spike.
    """
    _forget(config, check)
    try:
        alarm = aws_client.find_alarm(check.target)
    except Exception as e:                              # noqa: BLE001
        log(f"follow-up {check.key}: could not read alarm {check.target} "
            f"({type(e).__name__}: {e})")
        return
    namespace, metric = alarm.get("Namespace", ""), alarm.get("MetricName", "")
    stat = alarm.get("Statistic") or "Sum"
    dims = {d["Name"]: d["Value"] for d in (alarm.get("Dimensions") or [])}
    # 24h, not 3h. A throttling burst is spiky and this metric goes silent
    # between bursts: the first re-check on a real run graphed a 3h window, found
    # no datapoints, and correctly refused to draw an empty chart — so the reply
    # carried no picture at all. A day-wide window shows the burst AND the quiet
    # stretch since, which is exactly the "has it come back" question.
    window_hours = int(os.environ.get("FOLLOW_UP_CW_HOURS", "24"))
    numbers = aws_server.tool_cloudwatch_metric_values(
        namespace, metric, alarm=check.target, dimensions=dims or None, stat=stat,
        hours=window_hours)

    # If the window is silent, widen ONCE rather than posting a re-check with no
    # picture: this metric goes quiet between bursts (verified — no datapoints at
    # all in the last 24h while the last burst was three days earlier), and the
    # most recent burst is still the useful thing to look at. The reply says
    # which window it is, so a wider graph can never read as "this is now".
    graph_hours = window_hours
    if "NO DATAPOINTS" in numbers:
        graph_hours = int(os.environ.get("FOLLOW_UP_CW_FALLBACK_HOURS", "168"))

    out_dir = Path(config.evidence_dir) / "followups"
    out_dir.mkdir(parents=True, exist_ok=True)
    original_dir = aws_server.EVIDENCE_DIR
    aws_server.EVIDENCE_DIR = out_dir
    images: List[Path] = []
    try:
        key = f"{check.key}-{metric.lower()}"
        answer = aws_server.tool_cloudwatch_metric_graph(
            namespace, metric, alarm=check.target, dimensions=dims or None, stat=stat,
            hours=graph_hours, key=key,
            title=f"{metric} ({stat}) — re-check +"
                  f"{minutes_for(check.kind)[check.attempt - 1]:g}m, last {graph_hours}h")
        path = out_dir / f"{key}.png"
        if answer.startswith("OK") and path.exists():
            images.append(path)
        else:
            log(f"follow-up {check.key}: no graph — {answer.splitlines()[0]}")
    finally:
        aws_server.EVIDENCE_DIR = original_dir

    schedule = minutes_for(check.kind)
    minutes = schedule[check.attempt - 1] if check.attempt <= len(schedule) else 0
    state = alarm.get("StateValue", "?")
    lines = [f"*Re-check (+{minutes:g}m):* alarm state `{state}`",
             f"*Alarm:* `{check.target}`",
             f"*Metric:* `{namespace}` `{metric}` (`{stat}`), window `{window_hours}h`"]
    for line in numbers.splitlines():
        stripped = line.strip()
        for label in ("datapoints", "latest", "peak", "total", "average"):
            if stripped.startswith(label + ":"):
                lines.append(f"*{label.capitalize()}:* `{stripped.split(':', 1)[1].strip()}`")
    if "NO DATAPOINTS" in numbers:
        lines.append(f"*Data:* none published in the last `{window_hours}h` — not a zero "
                     f"reading, the metric is silent")
        if images:
            lines.append(f"*Graph window:* `{graph_hours}h`, to show the most recent activity")
    log(f"follow-up {check.key}: cloudwatch re-check, state {state}, {len(images)} graph(s)")
    _post(check, config, client, "\n".join(lines), images, log)
    # Post at every attempt: arm the next one regardless of what this said.
    if not check.is_final:
        _reschedule(check, config, client, log)


def _panel_params(alert, log: Callable[[str], None]) -> Dict[str, str]:
    """The mapped Grafana panel for this alert, resolved to concrete variables.

    config/panel_map.json already knows how to turn an alert's labels into panel
    variables ({label:2} -> the ESXi host_name), so this reuses it rather than
    inventing a second mapping. Empty dict means "no pinned panel" — the
    re-check then screenshots the Thanos UI instead.
    """
    try:
        from . import evidence_panels

        panel_map = evidence_panels.load_panel_map()
        for key in [k for k in (alert.alert_name, alert.incident_name) if k]:
            specs = panel_map.specs_for(key, alert.environment_key,
                                        labels=alert.labels, hints=alert.label_hints)
            if specs:
                spec = specs[0]
                return {"dashboard_uid": spec.dashboard_uid, "panel_id": str(spec.panel_id),
                        "from": spec.from_, "to": spec.to,
                        "variables": json.dumps(spec.variables)}
    except Exception as e:                              # noqa: BLE001 — hint, not a requirement
        log(f"follow-up: no mapped panel for {alert.alert_name!r} ({e})")
    return {}


# A panel variable names the same thing as a series label, but not by the same
# name. Only the mappings the pinned dashboards actually use are listed — a
# guessed mapping would scope a query to a label that does not exist, return
# nothing, and read as "recovered".
_VARIABLE_TO_LABEL = {
    "var-esxhost": "host_name",
    "var-vm": "vm_name",
    "var-instance": "instance",
    "var-host": "instance",
    "var-hostname": "hostname",
    "var-volume": "persistentvolumeclaim",
    "var-namespace": "namespace",
}

# Function names that must never gain a label selector.
_PROMQL_FUNCS = {"sum", "max", "min", "avg", "count", "rate", "irate", "increase", "without",
                 "by", "topk", "bottomk", "abs", "ceil", "floor", "round", "clamp_max",
                 "clamp_min", "delta", "deriv", "histogram_quantile", "label_replace",
                 "label_join", "time", "vector", "scalar", "absent", "changes", "quantile",
                 "offset", "ignoring", "group_left", "group_right", "unless", "and", "or"}


def _scope_selector(params: Dict[str, str]) -> str:
    """A label selector for the ONE resource the alert named, from the panel vars.

    Why this exists: the ESXi rules select a whole CLUSTER
    (`cluster_name=~"RealMatch-Cluster01|RealMatch-Cluster03"`), so max() over
    the rule's own expression reports the worst host in the cluster. On a real
    run the investigation caught this itself — "the rule selector is cluster-wide,
    so its 6h peak of 105.6% is ny1esx9681, not the host named here" — while the
    re-check quoted 105.63 as though it were this host's. Scoping fixes that.
    """
    try:
        variables = json.loads(params.get("variables") or "{}")
    except ValueError:
        return ""
    for variable, value in variables.items():
        label = _VARIABLE_TO_LABEL.get(variable)
        if label and value:
            return f'{label}="{value}"'
    return ""


def _scoped_expr(expr: str, scope: str) -> str:
    """Add a label selector to every metric selector in a PromQL expression.

    Both halves of `(vmware_host_cpu_usage{...} / vmware_host_cpu_max) * 100`
    need the host, or the division mixes two different machines.
    """
    if not scope:
        return expr

    def inject(match):
        name, braces = match.group(1), match.group(2)
        if name in _PROMQL_FUNCS:
            return match.group(0)
        if braces:
            return f"{name}{{{scope},{braces[1:-1]}}}"
        return f"{name}{{{scope}}}"

    return re.sub(r"\b([a-z_][a-z0-9_]{3,})(\{[^}]*\})?", inject, expr)


def _run_thanos(check: PendingCheck, config, client, log: Callable[[str], None]) -> None:
    """Re-read the rule's metric and post the number, the explanation and a graph.

    Posts at EVERY attempt (owner: "take a screenshot periodically"), so the
    thread shows the shape over the 5-10 minutes after the alert rather than one
    snapshot. The wording is a fixed template built from thanos.BreachSummary —
    "high for 2h10m" / "low again, came back below 12m ago" is computed from the
    series, never phrased by a model.
    """
    _forget(config, check)
    try:
        rules = thanos.find_alert_rules(check.target)
    except Exception as e:                              # noqa: BLE001
        log(f"follow-up {check.key}: Thanos unreachable ({type(e).__name__}: {e})")
        return
    if not rules:
        log(f"follow-up {check.key}: rule {check.target!r} no longer found")
        return
    rule = rules[0]
    # The first metric-looking identifier, not the first token: an expression
    # starting with "(" produced the label "value" on a real run.
    names = [n for n in re.findall(r"[a-z_][a-z0-9_]{3,}", rule.expr)
             if n not in _PROMQL_FUNCS]
    metric = names[0] if names else "value"
    scope = (check.params or {}).get("scope") or ""
    expr = _scoped_expr(rule.expr, scope)
    hours = int(os.environ.get("FOLLOW_UP_THANOS_HOURS", "6"))
    try:
        summary = thanos.breach_summary(expr, rule.threshold, hours=hours)
    except Exception as e:                              # noqa: BLE001
        log(f"follow-up {check.key}: no series ({type(e).__name__}: {e})")
        return

    out_dir = Path(config.evidence_dir) / "followups"
    out_dir.mkdir(parents=True, exist_ok=True)
    original_dir = grafana_server.EVIDENCE_DIR
    grafana_server.EVIDENCE_DIR = out_dir
    images: List[Path] = []
    try:
        key = f"{check.key}-{metric}"
        panel = check.params or {}
        if panel.get("dashboard_uid"):
            # A pinned dashboard panel beats the Thanos UI: it is the graph the
            # on-call engineer actually opens for this alert type.
            answer = grafana_server.tool_render_panel(
                panel["dashboard_uid"], int(panel["panel_id"]),
                variables=json.loads(panel.get("variables") or "{}"),
                from_=panel.get("from") or "now-15m", to=panel.get("to") or "now", key=key)
        else:
            answer = grafana_server.tool_thanos_graph(
                rule.expr, hours=hours, key=key, threshold=rule.threshold or 0.0)
        path = out_dir / f"{key}.png"
        if answer.startswith("OK") and path.exists():
            images.append(path)
        else:
            log(f"follow-up {check.key}: no graph — {answer.splitlines()[0]}")
    finally:
        grafana_server.EVIDENCE_DIR = original_dir

    schedule = minutes_for(check.kind)
    minutes = schedule[check.attempt - 1] if check.attempt <= len(schedule) else 0
    headline = ("still high" if summary.above_now else
                "low again" if summary.maximum > (rule.threshold or 0) else "still low")
    lines = [f"*Re-check (+{minutes:g}m):* {headline}",
             f"*{metric}:* `{_metric_number(summary.current)}`"
             + (f" (threshold `{_metric_number(rule.threshold)}`)"
                if rule.threshold is not None else ""),
             f"*Explanation:* {summary.explain()}",
             f"*Window:* `{hours}h`, `{summary.points}` datapoint(s), "
             f"min `{_metric_number(summary.minimum)}` / max `{_metric_number(summary.maximum)}`"]
    if (check.params or {}).get("dashboard_uid"):
        lines.append(f"*Panel:* `{check.params['panel_id']}` on `{check.params['dashboard_uid']}` "
                     f"(`{check.params.get('from', 'now-15m')}`)")
    log(f"follow-up {check.key}: thanos re-check, {metric}={summary.current}, "
        f"{len(images)} graph(s)")
    _post(check, config, client, "\n".join(lines), images, log)
    if not check.is_final:
        _reschedule(check, config, client, log)


def _metric_number(value) -> str:
    if value is None:
        return "unknown"
    return f"{int(round(value))}" if abs(value - round(value)) < 0.01 else f"{value:.2f}"


# "Application Load Balancer app/uk-prod-fastcore-app-http/992c86a8ba9f5fb3 has
# at least 1 unhealthy instances" — the ARN tail is in the summary, which is the
# only place the balancer is named.
_ALB_IN_TEXT = re.compile(r"\b(?:app|net)/[A-Za-z0-9-]+/[0-9a-f]{8,}", re.I)


def _load_balancer_for(alert, log: Callable[[str], None]) -> str:
    """The load balancer this alert names, confirmed against AWS before arming."""
    override = os.environ.get("FOLLOW_UP_ALB", "").strip()
    if override:
        return override
    text = f"{alert.alert_name or ''} {alert.incident_name or ''} {alert.raw_text or ''}"
    for candidate in dict.fromkeys(_ALB_IN_TEXT.findall(text)):
        try:
            aws_client.find_load_balancer(candidate)
            return candidate
        except Exception:                               # noqa: BLE001 — try the next match
            continue
    log(f"follow-up not scheduled: no load balancer ARN tail (app/<name>/<id>) in "
        f"{alert.alert_name!r} could be confirmed in "
        f"{', '.join(aws_client.profiles())}")
    return ""


def _evidence_paths(answer: str, out_dir: Path) -> List[Path]:
    """Every file a render tool actually wrote, from the `evidence_key:` lines it
    reports. More reliable than rebuilding the filename, because the tools are
    free to slugify their keys."""
    paths = []
    for line in (answer or "").splitlines():
        if line.startswith("evidence_key:"):
            candidate = out_dir / f"{line.split(':', 1)[1].strip()}.png"
            if candidate.exists():
                paths.append(candidate)
    return paths


def _run_alb(check: PendingCheck, config, client, log: Callable[[str], None]) -> None:
    """Re-read target health and re-render the healthy/unhealthy graph.

    Posts at EVERY attempt on the 3/6-minute cadence: the owner asked to look
    again after 2-3 minutes because that is exactly the window in which a
    warming-up target either passes its health checks or does not.
    """
    _forget(config, check)
    health = aws_server.tool_alb_target_health(check.target)
    if health.startswith("LOOKUP FAILED"):
        log(f"follow-up {check.key}: {health.splitlines()[0]}")
        return

    out_dir = Path(config.evidence_dir) / "followups"
    out_dir.mkdir(parents=True, exist_ok=True)
    original_dir = aws_server.EVIDENCE_DIR
    aws_server.EVIDENCE_DIR = out_dir
    images: List[Path] = []
    try:
        answer = aws_server.tool_alb_health_graph(check.target, hours=3,
                                                  key=f"{check.key}_alb")
        # Read the key back out of the answer rather than rebuilding the path:
        # the tool slugifies its key (dashes become underscores), so
        # "994144-1_alb" is saved as "994144_1_alb.png" and a reconstructed path
        # missed a graph that had rendered fine (real failure, 2026-08-31).
        for path in _evidence_paths(answer, out_dir):
            images.append(path)
        if not images:
            log(f"follow-up {check.key}: no graph — {answer.splitlines()[0]}")
    finally:
        aws_server.EVIDENCE_DIR = original_dir

    _schedule = minutes_for(check.kind)
    minutes = _schedule[check.attempt - 1] if check.attempt <= len(_schedule) else 0
    # Pull the per-group counts straight out of the tool's own output rather than
    # re-deriving them, so the reply cannot disagree with the evidence.
    counts = [line.strip() for line in health.splitlines()
              if "healthy /" in line or "UNHEALTHY" in line]
    unhealthy_lines = [line.strip() for line in health.splitlines() if line.strip().startswith("!!")]
    lines = [f"*Re-check (+{minutes:g}m):* "
             + ("still unhealthy" if unhealthy_lines else "all targets healthy"),
             f"*Load balancer:* `{check.target}`"]
    for line in counts[:3]:
        lines.append(f"*Targets:* {line}")
    for line in unhealthy_lines[:4]:
        lines.append(f"*Unhealthy:* `{line[3:]}`")
    log(f"follow-up {check.key}: alb re-check, {len(unhealthy_lines)} unhealthy target(s), "
        f"{len(images)} graph(s)")
    _post(check, config, client, "\n".join(lines), images, log)
    if not check.is_final:
        _reschedule(check, config, client, log)


def _reschedule(check: PendingCheck, config, client, log) -> None:
    schedule = minutes_for(check.kind)
    next_attempt = check.attempt + 1
    if next_attempt > len(schedule):
        return
    delay = schedule[next_attempt - 1] - schedule[check.attempt - 1]
    later = PendingCheck(**{**asdict(check), "attempt": next_attempt,
                            "due_at": time.time() + max(0.0, delay) * 60})
    _persist(config, later)
    log(f"follow-up {later.key}: next look in {max(0.0, delay):g}m")
    _arm_timer(later, config, client, log)


def _post(check: PendingCheck, config, client, message: str, images: List[Path],
          log: Callable[[str], None]) -> None:
    images = [i for i in images if i and i.exists()]
    if not (config.is_live and client):
        log(f"\n[DRY RUN] would reply in thread {check.thread_ts}:\n{message}"
            + (f"\n  files: {[str(i) for i in images]}" if images else ""))
        return
    try:
        if images:
            # One upload call with both panels, so they land together under one
            # comment rather than as two replies.
            client.files_upload_v2(
                channel=check.channel, thread_ts=check.thread_ts,
                initial_comment=message,
                file_uploads=[{"file": str(i), "filename": i.name} for i in images],
            )
        else:
            client.chat_postMessage(channel=check.channel, thread_ts=check.thread_ts,
                                    text=message)
        log(f"follow-up {check.key}: posted into thread {check.thread_ts}")
    except Exception as e:                              # noqa: BLE001
        log(f"follow-up {check.key}: could not post ({type(e).__name__}: {e})")


def wait_for_pending(timeout_seconds: float = 0) -> None:
    """Block until armed timers have fired — for one-shot runs (replay_alert,
    --once), where the process would otherwise exit before the re-check.
    A long-running listener does not need this."""
    longest = max([m for k in list(_KIND_MINUTES) + ['default']
               for m in minutes_for(k)] or [0])
    deadline = time.time() + (timeout_seconds or (longest * 60 + 120))
    while time.time() < deadline:
        alive = [t for t in threading.enumerate() if t.name.startswith("followup-")]
        if not alive:
            return
        time.sleep(2)
