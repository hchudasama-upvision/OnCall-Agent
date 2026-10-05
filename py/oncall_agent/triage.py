import json
from typing import List, Optional

from . import llm
from .slack_history import HistoryFetchResult
from .types import ParsedAlert

"""
The generic triage path: every alert type that does NOT have a deterministic
evidence pipeline yet (i.e. everything except engine-failure rate, which
goes through the Edge UI pipeline + decide_resolution.py).

Produces the 7-section senior-engineer report developed in the noc-ai-lab
prototype, extended with a fifth labelled input — PAST_THREADS, the real
recent #comms-noc threads for this alert type (slack_history.py). The
prototype only had the offline case library; this repo can read the live
channel, and "investigate based on what #comms-noc did last time" is the
whole point.

Every input is LABELLED, including the empty ones. That is not cosmetic: an
omitted section invites the model to invent a change record, and "never
fabricate a permalink" only binds if it knows the feed is empty by design.
"""

SYSTEM_PROMPT = """You are a senior NOC triage engineer. For each incoming alert you receive:
1. ALERT: the raw alert payload
2. PAST_CASES: structured records of how our team resolved similar alerts
   (may be empty)
3. PAST_THREADS: real recent #comms-noc threads for this alert type — what
   the on-call engineer actually posted, investigated and escalated to
   (may be empty or unavailable)
4. RECENT_CHANGES: candidate change records from the last 14 days - Slack
   messages, tickets, CloudTrail events - retrieved by keyword match
   (may be empty, may contain irrelevant items)
5. OBSERVATIONS: live command/tool outputs and evidence already gathered,
   if any

Produce a triage report with EXACTLY these sections:

*What fired* - one plain-language line. If the alert groups multiple
items ([FIRING:N]), state whether they look like N problems or one
shared cause, and why.

*Seen before* - only from PAST_CASES and PAST_THREADS: occurrences,
typical resolution, usual fix, and what the on-call engineer did last
time. If both are empty: "No matching history."

*Possibly related changes* - examine RECENT_CHANGES. Include an item
ONLY if you can name the specific overlap (service, cluster, host,
timing). Format: [link] date, author, one-line summary, and HOW it
could explain this alert. Label each: LIKELY CAUSE / POSSIBLY RELATED /
checked-and-unrelated (mention the last only if a human would
plausibly waste time on it). If nothing overlaps: "No recorded changes
match." Never force a connection.

*First steps* - numbered, read-only diagnostics, most decisive first.
Prefer exact commands from PAST_CASES. Design steps to DISCRIMINATE
between hypotheses (e.g., "curl from a second vantage point - if it
works externally, the path is X; if not, Y").

*Hypotheses* - this is where you think beyond the given context.
Rank 2-4 plausible explanations. For each: mechanism (how it would
produce exactly these symptoms), what evidence supports/contradicts it
so far, and the single cheapest check that would confirm or kill it.
You MUST consider, when the pattern fits:
- INTENDED STATE: was this deliberately turned off/scaled down/
  decommissioned? (check for capacity=0, steady-state-at-zero,
  age of the failure vs. age of the page)
- SHARED PATH: multiple unrelated things failing together = suspect
  the common layer (LB, ingress, tunnel, DNS, prober network),
  never N independent failures
- MONITORING ITSELF: the prober, its network, or its config may be
  what broke; "down" may mean "unobservable"
- TIMING: what is the TRUE start time of the failure vs. when the
  alert fired? A 3-day-old failure paging tonight is a different
  investigation than a fresh one
- DEPENDENCY: the failing component may be a victim; name the
  upstream suspect (e.g., health checks that call a dependency)
Mark each hypothesis with confidence: HIGH / MEDIUM / SPECULATIVE.

*Suggested remediation* - from PAST_CASES/PAST_THREADS or standard
practice, always marked "REQUIRES HUMAN APPROVAL". Never claim you
executed anything. If intended-state is plausible, remediation is
"verify intent with the actor, then fix monitoring" - NOT "turn it
back on".

*Escalate if* - concrete conditions and to whom. Prefer the team that
PAST_THREADS shows was actually escalated to for this alert type, then
PAST_CASES escalation fields.

HARD RULES:
- Separate observation from inference. Facts come only from ALERT,
  PAST_CASES, PAST_THREADS, RECENT_CHANGES, OBSERVATIONS. Everything
  else you state must be phrased as hypothesis with confidence level.
- Never invent: commands not in cases/standard tooling, hostnames,
  metrics values, past incidents, or change records. Never fabricate
  a permalink or a URL - the only links you may use are ones that
  appear verbatim in the inputs above.
- Quote error text precisely when reasoning from it ("awaiting headers"
  means the connection succeeded - reason at that level of detail).
- If OBSERVATIONS contradict a PAST_CASES playbook, say so explicitly
  and prefer the observations.
- If the evidence is insufficient to rank hypotheses, your first step
  is the question or command that best splits the space - say which
  hypothesis each outcome supports.
- Terse ops tone, Slack mrkdwn, no preamble, no reassurance filler."""

# Inputs the prompt expects but nothing gathers yet. Say so explicitly rather
# than omitting the section (see module docstring).
NO_CHANGE_FEED = ("none - no change feed is wired (no Slack/ticket/CloudTrail "
                  "retrieval exists yet). Treat as empty; do not infer changes.")
NO_OBSERVATIONS = ("none - no live command output was gathered for this alert "
                   "type yet. Treat as empty; every diagnostic below is still unrun.")


def _blob(value) -> str:
    """Render one prompt input: JSON for structured data, verbatim for text."""
    if not value:
        return ""
    return value if isinstance(value, str) else json.dumps(value, indent=2)


def format_history(history: Optional[HistoryFetchResult], max_threads: int = 5,
                   max_replies: int = 10) -> str:
    """Real #comms-noc threads as prompt text, or an explicit reason there are none."""
    if history is None:
        return "(not fetched)"
    if history.unavailable_reason:
        return (f"(unavailable: {history.unavailable_reason}. Decide from the current "
                f"evidence and PAST_CASES alone - do not invent past incidents.)")
    if not history.threads:
        return "(no matching past #comms-noc threads found)"
    parts = []
    for i, thread in enumerate(history.threads[:max_threads], 1):
        replies = "\n".join(f"    reply: {r}" for r in thread.reply_texts[:max_replies])
        link = f" ({thread.permalink})" if thread.permalink else ""
        parts.append(f"  [{i}]{link} top-level: {thread.top_level_text}\n{replies}")
    return "\n".join(parts)


def build_user_message(
    alert_text: str,
    past_cases: List[dict],
    history: Optional[HistoryFetchResult] = None,
    recent_changes=None,
    observations=None,
) -> str:
    return (
        f"ALERT:\n{alert_text}\n\n"
        f"PAST_CASES (structured, from our own incident history):\n"
        f"{_blob(past_cases) or 'none'}\n\n"
        f"PAST_THREADS (real recent #comms-noc threads for this alert type):\n"
        f"{format_history(history)}\n\n"
        f"RECENT_CHANGES:\n{_blob(recent_changes) or NO_CHANGE_FEED}\n\n"
        f"OBSERVATIONS:\n{_blob(observations) or NO_OBSERVATIONS}"
    )


def triage(
    alert_text: str,
    past_cases: List[dict],
    history: Optional[HistoryFetchResult] = None,
    recent_changes=None,
    observations=None,
) -> str:
    """The 7-section triage report as Slack mrkdwn."""
    return llm.complete(SYSTEM_PROMPT, build_user_message(
        alert_text, past_cases, history, recent_changes, observations))


# A NodeHighCPUUsage card can carry 9 sub-alerts and a KubePodsNotReady card
# 230. They repeat the same shape, so the prompt gets a bounded sample plus the
# real counts rather than a payload that crowds out the case library.
_MAX_SUB_ALERTS = 8


def alert_prompt_text(alert: ParsedAlert) -> str:
    """The ALERT input: the raw payload plus what the parser pulled out of it.

    Both, not just the parse — the model reasons from exact error strings,
    and a normalized view alone would hide whatever the parser missed.
    """
    parsed = {
        "source": alert.source,
        "kind": alert.kind,
        "incident_number": alert.incident_number or None,
        "incident_name": alert.incident_name or None,
        "alert_name": alert.alert_name or None,
        "environment": alert.environment_key or None,
        "entity": alert.entity_display_name or None,
        "monitoring_tool": alert.monitoring_tool or None,
        "state_message": alert.state_message or None,
        "escalation_policy": alert.escalation_policy or None,
        "firing_count": alert.firing_count or None,
    }
    parsed = {k: v for k, v in parsed.items() if v is not None}

    # The sub-alerts, as STRUCTURE rather than as a blob inside raw_text. The
    # card's state_message carries one block per sub-alert with the label
    # NAMES spelled out — `node`, `pod`, `dbinstance_identifier`,
    # `load_balancer`, `engineName` — which is what turns "investigate this
    # alert" into "investigate THIS resource". Firing ones are listed first
    # and labelled, because a card routinely mixes firing and resolved and the
    # resolved ones are rendered at the top.
    blocks = ""
    if alert.sub_alerts:
        firing = alert.firing_sub_alerts
        ordered = firing + [s for s in alert.sub_alerts if s not in firing]
        parsed["sub_alert_count"] = len(alert.sub_alerts)
        parsed["firing_sub_alert_count"] = len(firing)
        shown = ordered[:_MAX_SUB_ALERTS]
        blocks = ("\n\n---\nsub-alerts parsed from state_message "
                  f"({len(firing)} firing of {len(alert.sub_alerts)}"
                  + (f", showing {len(shown)}" if len(shown) < len(alert.sub_alerts) else "")
                  + "):\n" + json.dumps(shown, indent=2))
        if any(s.get("truncated") for s in shown):
            blocks += ("\n\nNOTE: a block is marked truncated — VictorOps cut the card off "
                       "mid-block. Its missing fields are UNKNOWN, not empty; do not fill them in.")
    return (f"{alert.raw_text}\n\n---\nparsed fields: {json.dumps(parsed, indent=2)}"
            f"{blocks}")
