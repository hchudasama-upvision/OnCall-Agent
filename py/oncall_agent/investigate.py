import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from .Agents.Grafana_Agent import server as grafana_mcp
from .agent_memory import format_memory
from .types import Decision, ParsedAlert, PlannedPost

"""
Investigation by tool use: Claude reads #comms-noc itself.

The older path (decide_resolution.py) keyword-prefetches a handful of threads
and hands them over as prompt text. That works, but it can only find what the
keyword guessed. This path gives the model read-only Slack tools
(slack_mcp_server.py) and asks it to go looking — search for the alert type,
open the threads, and read what was actually DONE — the way a person picks up
an unfamiliar page.

Headless `claude -p` cannot use the claude.ai Slack connector (verified
2026-08-24: it answers NO_SLACK_TOOLS), so the tools come from our own MCP
server over --mcp-config. Same investigation, a token we control, and a
read-only surface: nothing reachable from a prompt can post.

No-fabrication guarantee, preserved
-----------------------------------
Letting the model fetch its own sources would normally destroy the guarantee
that every link in a posted thread is real. It does not here, because the run
uses --output-format stream-json: every tool_result the model received is
captured, and any URL in the final output that does not appear verbatim in
that corpus (or in the alert itself) aborts the post. The model can cite what
it read and nothing else.
"""

# Read-only by construction — the server exposes no write tool, and this list
# is the second lock: even if one were added, it could not be called here.
SLACK_TOOLS = [
    "mcp__noc_slack__read_channel",
    "mcp__noc_slack__read_thread",
    "mcp__noc_slack__search_messages",
    "mcp__noc_slack__list_channels",
    "mcp__noc_slack__search_recent_changes",
]

# Evidence gathering is a LIVE investigation, not a lookup table. The model
# searches Grafana, reads what a panel actually queries, checks the metric
# exists for this host, and renders it — the loop a human runs. An empty
# render comes back as "EMPTY" so it can correct itself, which a static
# alert->panel mapping could never do.
GRAFANA_TOOLS = [
    "mcp__noc_grafana__search_dashboards",
    "mcp__noc_grafana__describe_dashboard",
    "mcp__noc_grafana__prometheus_query",
    "mcp__noc_grafana__list_datasources",
    "mcp__noc_grafana__api_success_rate",
    "mcp__noc_grafana__thanos_alert_status",
    "mcp__noc_grafana__thanos_graph",
    "mcp__noc_grafana__render_panel",
]

ALL_TOOLS = SLACK_TOOLS + GRAFANA_TOOLS

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MCP_CONFIG = _REPO_ROOT / ".mcp.json"

_URL = re.compile(r"https?://[^\s|>)\]]+")


def _evidence_dir() -> Path:
    return Path(os.environ.get("EVIDENCE_DIR") or (_REPO_ROOT / "dist" / "evidence"))


def _run_evidence_dir(alert: ParsedAlert) -> Path:
    """A directory unique to THIS investigation.

    Concurrent runs previously shared dist/evidence and its manifest, so a
    second run's clear_manifest() wiped the first run's rendered panels — and
    worse, a run could load a panel rendered for a DIFFERENT alert and attach
    it as its own evidence. Wrong graph, right-looking thread. Isolating per
    run makes several terminals safe to use at once.
    """
    tag = f"{alert.incident_number or 'alert'}-{os.getpid()}"
    path = _evidence_dir() / "runs" / tag
    path.mkdir(parents=True, exist_ok=True)
    return path


def _schema(evidence_keys: List[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "should_post": {
                "type": "boolean",
                "description": "False when the evidence is too thin, the incident already "
                               "auto-resolved with no action possible, or #comms-noc shows "
                               "this alert type is knowingly ignored.",
            },
            "reasoning": {
                "type": "string",
                "description": "Internal — audit log only, never posted to Slack.",
            },
            "prior_incidents": {
                "type": "array",
                "description": "Message timestamps of the past #comms-noc threads this "
                               "investigation was grounded in. Empty if none were found. "
                               "Never invent one — copy the ts values the tools returned.",
                "items": {"type": "string"},
            },
            "root_cause_narrative": {
                "type": "string",
                "description": "Short plain-language cause, or empty string if the evidence "
                               "does not support a confident explanation.",
            },
            "owning_team_mention": {
                "type": "string",
                "description": "Slack mention of the team to escalate to, e.g. "
                               "'<@U02H8JZKVFV>' or '@engines'. Ground it in who past "
                               "threads actually escalated to. Empty string if unclear.",
            },
            "proposed_action": {
                "type": "object",
                "description": "LEAVE summary EMPTY. The thread is observations only "
                               "(owner's direction, 2026-08-27): the on-call engineer reads "
                               "the numbers and decides. Kept in the schema because the "
                               "approve/deny plumbing still exists and slack_post gates it "
                               "off; it is not a field for you to fill.",
                "properties": {
                    "summary": {"type": "string",
                                "description": "One line: what should be done and to what. "
                                               "Empty string if none."},
                    "command": {"type": "string",
                                "description": "The exact command a human would run, if there "
                                               "is one. Empty string otherwise. It will be "
                                               "posted for a person to run, never executed."},
                    "risk": {"type": "string",
                             "description": "What this affects if it goes wrong, and whether "
                                            "it is reversible."},
                },
                "required": ["summary", "command", "risk"],
            },
            "posts": {
                "type": "array",
                "description": "The thread replies to post, in order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        # No enum: with live Grafana tools the model CREATES
                        # evidence during the run, so the valid keys are not
                        # knowable when the schema is built. _validate checks
                        # every cited key against what was actually rendered.
                        "evidence_keys": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["text", "evidence_keys"],
                },
            },
        },
        "required": ["should_post", "reasoning", "prior_incidents", "root_cause_narrative",
                     "owning_team_mention", "proposed_action", "posts"],
    }


# The same two rules every specialist gets (Agents/shared_prompt.py), so the
# generalist fallback and the no-tools path cannot drift into a looser standard
# than the routed agents. shared_prompt imports nothing, so this is cycle-free.
from .Agents.shared_prompt import (  # noqa: E402
    BACKTICK_VALUES,
    CONFIRMED_ONLY,
    EVIDENCE_ECONOMY,
)

_HOUSE_STYLE = """HOUSE STYLE — match it, this is not a report:
Real #comms-noc threads are short, factual, incremental. Actual examples:
    "100% CPU utilization on VM"
    "Can't login"
    "*Restarting* the VM *STG-Backend5*"
    "Seeing C: drive is almost full, 99.7% utilized."
    "Mailed XGlobe to assist on this."
    "<@U02H8JZKVFV|Anton Asnitsky> FYI"
One observation per reply. Slack mrkdwn. No headings, no bullet-point essays, no
preamble, no reassurance. If you would write a paragraph, you are writing the
wrong thing.

Every line is something you OBSERVED — there is no recommendation half to the
thread (see OBSERVATIONS ONLY below). You have not run any command and have not
changed anything — never write as though you did.

The last reply carries the escalation @mention, when there is one to make.

WHAT YOU MAY AND MAY NOT DO — this is absolute:
- READ-ONLY. You may look at anything: read Slack, query Prometheus, render
  Grafana panels, gather facts. That is the whole of your authority.
- You may NOT change anything, and you have no tool that could. No restarting
  pods, no deleting anything, no resizing, no scaling, no config edits, no
  running commands on a host. Not even something small and obviously safe.
- You do not recommend either. Report the numbers; the on-call engineer decides
  what to do about them. proposed_action's summary stays empty.
- Never write a reply that implies you acted. A NOC engineer reading the thread
  must never be misled about whether the cluster has already been touched.

HARD RULES:
- Every URL you write must be copied verbatim from something a tool returned or
  from the alert itself. Never construct, guess, or complete a URL. A run that
  emits an invented link is discarded entirely.
- Every ts in prior_incidents must be one a tool actually returned.
- Never state a metric, hostname, task count, org or error string that is not in
  the alert or in a tool result. Say what you do not know.
- If past threads show this alert type is knowingly ignored (chronic staging
  noise, a pending deploy, auto-resolves with no action possible), say so and set
  should_post accordingly — a thread that adds nothing is worse than silence.

""" + CONFIRMED_ONLY + """

""" + EVIDENCE_ECONOMY + """

""" + BACKTICK_VALUES


_ROLE = ("You are the NOC on-call agent for Veritone. A VictorOps incident has just "
         "paged. Investigate it the way the on-call engineer does, then draft what "
         "should be posted in the incident's #comms-noc thread.")

# With tools: the model goes and finds its own grounding.
SYSTEM_PROMPT = f"""{_ROLE}

You have READ-ONLY Slack tools. Use them — do not answer from assumption:

1. Find out how this alert type was handled before. search_messages with modifiers
   (in:#comms-noc, the alertname, the environment) is fastest; read_channel on
   #comms-noc works when search is unavailable.
2. Open the promising threads with read_thread. The thread is where the real
   content is: what was checked, what was actually DONE, which team was pulled in,
   and whether it turned out to be a known/ignored condition.
3. Check #alerts-devops around the incident's timestamp for other alerts firing at
   the same time. The same real-world condition often arrives two or three times
   (local + central Alertmanager, plus the VictorOps mirror), and unrelated things
   failing together usually means one shared layer, not N failures.

GRAFANA EVIDENCE — for resource alerts (memory high, PVC/disk full, CPU high),
the graph IS the answer. Go and get it:

4. search_dashboards for the resource in the alert. describe_dashboard on the
   best match — READ THE PANEL QUERIES. A panel titled "Memory Utilization" may
   filter on $instance while the dashboard also offers $hostname; setting the
   obvious variable then renders an empty panel.
5. prometheus_query to confirm the metric exists for THIS host, and to
   translate what the alert gives you into what the panel wants. Example: the
   alert says host SVC182, the panel wants an instance, so
   windows_os_hostname{{hostname="SVC182"}} gives instance=10.60.4.182:9182.
6. render_panel with those variables. If it returns EMPTY, the variables
   matched nothing — do not attach it and do not describe it. Work out why
   (wrong variable? metric absent for this host?) and try again, or say plainly
   that no graph was available.

Render ONE panel — the one showing the alerting resource, not a fleet-wide view.
A second only if it answers something the first cannot; never more than two.

Then draft the thread.

{_HOUSE_STYLE}"""

# Without tools: grounding is whatever the caller could prefetch. Same house
# style and same guardrails, so both routes produce threads that read alike —
# the alternative was a second, essay-shaped format that matched nothing in
# the real channel.
CONTEXT_PROMPT = f"""{_ROLE}

You have NO tools. Everything you know is in the message below: the alert, any
past cases from our own incident history, any #comms-noc threads that could be
retrieved, and whatever else was firing nearby. Where a section says it is
unavailable, treat that as a gap in YOUR knowledge and say so in the thread —
never fill it in from assumption.

{_HOUSE_STYLE}"""


@dataclass
class Investigation:
    decision: Decision
    prior_incidents: List[str] = field(default_factory=list)
    tools_used: List[str] = field(default_factory=list)
    sources_seen: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    # Evidence the run PRODUCED (panels it rendered), keyed as cited.
    evidence_files: Dict[str, Path] = field(default_factory=dict)

    def to_json(self) -> dict:
        d = asdict(self.decision)
        d["prior_incidents"] = self.prior_incidents
        d["_audit"] = {
            "tools_used": self.tools_used,
            "tool_results": self.sources_seen,
            "cost_usd": self.cost_usd,
            "duration_ms": self.duration_ms,
        }
        return d


def format_cases(cases: Optional[List[dict]]) -> str:
    """What we already know about this alert type, for the prompt.

    This is the difference between the model re-deriving the same dashboard,
    the same label quirk and the same escalation every single run, and it
    going straight there. It is a HEAD START, not an instruction: the case may
    be stale, so the prompt tells it to verify rather than obey.
    """
    if not cases:
        return ("(no case-library entry for this alert type — you are investigating it "
                "from scratch. Say so if that limits what you can conclude.)")
    return json.dumps(cases, indent=2)


def build_prompt(alert: ParsedAlert, evidence_keys: List[str],
                 evidence_descriptions: Optional[Dict[str, str]] = None,
                 past_cases: Optional[List[dict]] = None,
                 memory_records: Optional[List[dict]] = None) -> str:
    link_line = (
        f"Incident link (the ONLY link you may use for this incident): {alert.incident_url}"
        if alert.incident_url
        else "This incident has no portal link available — refer to it as plain text."
    )
    graph_only = bool(evidence_keys) and all(k.startswith("grafana_") for k in evidence_keys)
    if evidence_keys:
        described = evidence_descriptions or {}
        menu = "\n".join(f"  - {k}: {described.get(k, 'evidence file')}" for k in evidence_keys)
        evidence_block = (
            "EVIDENCE FILES ALREADY CAPTURED (attach by naming the key in evidence_keys; "
            f"never name one that is not listed):\n{menu}"
        )
        if graph_only:
            # Resource alerts (memory high, PVC full, disk full) are answered by
            # the graph. Without this the model writes a diagnosis essay above a
            # picture that already says it, which is the opposite of the channel
            # convention and what the owner asked to stop.
            evidence_block += (
                "\n\nTHIS IS A GRAPH ALERT. The dashboard IS the answer, so keep it to "
                "AT MOST TWO replies:\n"
                "  1. one line naming what is high/full and where — the resource, the "
                "host/volume/VM, and the environment. Use only values present in the "
                "alert; you have no metric readings, so do not state a percentage.\n"
                "  2. the panel attached, with at most one short line of context. One "
                "panel, not a gallery.\n"
                "Add a third reply ONLY to @mention an escalation. No diagnosis "
                "walkthrough, no numbered steps, no hypotheses, and no reply about what "
                "you could not check — a human reads the graph faster than your "
                "description of it."
            )
    else:
        evidence_block = (
            "EVIDENCE FILES: none were captured for this alert type. Leave every "
            "evidence_keys array empty."
        )

    return f"""A VictorOps incident just paged the NOC. Investigate it and draft the #comms-noc thread.

INCIDENT
  Number:      {alert.incident_number or "(none)"}
  Title:       {alert.incident_name}
  Alert name:  {alert.alert_name or "(not parsed)"}
  Environment: {alert.environment_key or "(not stated)"}
  Source:      {alert.monitoring_tool or "(unknown)"}
  Phase/state: {alert.alert_phase or "?"} / {alert.alert_state or "?"}
  Acked by:    {alert.acked_by or "(not yet)"}
  Escalation:  {alert.escalation_policy or "(unknown)"}
  Fired at:    {alert.message_ts or "(unknown)"} (Slack ts in #alerts-devops)
  {link_line}

RAW ALERT PAYLOAD
{alert.raw_text}

{evidence_block}

WHAT WE ALREADY KNOW ABOUT THIS ALERT TYPE (our own case library — curated by
the NOC, not by you). Use it as a head start: it may name the dashboard, the
label quirk, the usual cause, who owns it. VERIFY before relying on it — a
case can be stale, and the live tools are the authority. If it turns out to be
wrong, say so in your reasoning so a human can correct the entry.
{format_cases(past_cases)}

YOUR OWN MEMORY OF PAST INVESTIGATIONS OF THIS EXACT ALERT TYPE (real outputs
YOU produced on prior runs — not curated by a human, not verified since they
were written). Same rule as the case library: a head start, not a fact.
Conditions drift between incidents — a root cause from last time may not be
this time's, an owning_team_mention may be stale. VERIFY against what your
tools show NOW before repeating anything below.
{format_memory(memory_records)}

Investigate with the Slack tools first — how has this alert type been handled in
#comms-noc before, and what was actually done? Then produce the decision."""


def draft_from_context(
    alert: ParsedAlert,
    past_cases: Optional[List[dict]] = None,
    history=None,
    correlation=None,
    evidence_keys: Optional[List[str]] = None,
    evidence_descriptions: Optional[Dict[str, str]] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
    claude_binary: Optional[str] = None,
) -> Investigation:
    """Draft the same terse thread with no tools, from prefetched context.

    This is the route for every alert type that has no deterministic evidence
    pipeline, and it deliberately shares the schema, house style and
    guardrails of investigate_alert(). The alternative — the 7-section
    report inherited from the noc-ai-lab prototype — produced a wall of
    headings that reads nothing like #comms-noc, where the real convention is
    one short observation per reply.

    The "corpus" for URL validation is the prompt itself: past threads passed
    in as text are legitimate things to quote, and nothing else is.
    """
    import shutil

    from .triage import format_history

    evidence_keys = list(evidence_keys or [])
    binary = claude_binary or os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not binary:
        raise RuntimeError("`claude` CLI not found — set CLAUDE_BIN or install Claude Code")

    grounding = (
        f"PAST CASES FROM OUR OWN INCIDENT HISTORY (curated, sanitized):\n"
        f"{json.dumps(past_cases, indent=2) if past_cases else '(none for this alert type)'}\n\n"
        f"PAST #comms-noc THREADS FOR THIS ALERT TYPE:\n{format_history(history)}\n\n"
        f"OTHER ALERTS FIRING NEARBY (correlation only — co-occurrence is not causation):\n"
        f"{correlation or '(not retrieved)'}"
    )
    prompt = f"{build_prompt(alert, evidence_keys, evidence_descriptions)}\n\n{grounding}"

    proc = subprocess.run(
        [binary, "-p", prompt,
         "--output-format", "json",
         "--system-prompt", CONTEXT_PROMPT,
         "--allowed-tools", "",
         "--json-schema", json.dumps(_schema(evidence_keys)),
         "--model", model or os.environ.get("CLAUDE_CLI_MODEL", "opus"),
         "--effort", effort or os.environ.get("CLAUDE_CLI_EFFORT", "low")],
        capture_output=True, text=True,
        timeout=timeout_seconds or int(os.environ.get("CLAUDE_CLI_TIMEOUT", "300")),
        cwd=tempfile.gettempdir(), stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI exited {proc.returncode}: {(proc.stderr or proc.stdout)[:400]}")
    envelope = json.loads(proc.stdout)
    if envelope.get("is_error"):
        raise RuntimeError(f"claude CLI reported an error: {str(envelope.get('result'))[:400]}")
    output = envelope.get("structured_output")
    if output is None:
        raise RuntimeError(f"No structured output returned: {proc.stdout[:400]}")

    decision, prior = _validate(output, alert, prompt, evidence_keys, past_cases)
    return Investigation(
        decision=decision, prior_incidents=prior, tools_used=[],
        sources_seen=len(getattr(history, "threads", []) or []),
        cost_usd=float(envelope.get("total_cost_usd") or 0.0),
        duration_ms=int(envelope.get("duration_api_ms") or 0),
    )


def _resolve_mcp_config(source: Path) -> Path:
    """Rewrite the MCP config to use THIS interpreter, and return the new path.

    The checked-in config says `"command": "python"`, which is only correct
    when the CLI happens to be spawned from an activated venv — otherwise the
    server starts under a system python with no slack_sdk and the tools
    silently do not appear. Substituting sys.executable makes the server run
    under whatever interpreter the agent itself is running under, which by
    construction has the dependencies.
    """
    config = json.loads(source.read_text())
    servers = config.get("mcpServers") or {}
    for spec in servers.values():
        if spec.get("command") in ("python", "python3"):
            spec["command"] = sys.executable
        # cwd is relative to the repo in the checked-in file; make it absolute
        # so the CLI's own working directory cannot change what it means.
        if spec.get("cwd"):
            spec["cwd"] = str((source.parent / spec["cwd"]).resolve())
    # Comment keys are for humans reading the file; the CLI rejects unknown
    # top-level keys, so drop them from what we actually hand it.
    resolved = {"mcpServers": servers}

    out_dir = Path(os.environ.get("STATE_DIR") or (_REPO_ROOT / ".state"))
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "mcp-resolved.json"
    out_path.write_text(json.dumps(resolved, indent=2))
    return out_path


# Signs in the CLI's output that the OAuth session, not the prompt, is the
# problem. `claude_cli` authenticates with the CLI's own login, and headless
# cannot run an OAuth flow — so an expired refresh token fails every run until
# someone types `claude` (or /login) interactively. See CLAUDE.md.
_AUTH_MARKERS = ("oauth", "unauthorized", "authentication_error", "invalid_api_key",
                 "please run /login", "credentials", "401")


def _diagnose_cli_failure(returncode: int, stdout: str, stderr: str) -> str:
    """A readable reason, not the stream-json init line.

    stream-json prints a big `{"type":"system","subtype":"init",...}` object
    listing every available tool BEFORE anything can fail, so slicing the head
    of stdout showed that inventory and hid the actual error. Take the last
    non-protocol lines instead, and name the auth case explicitly since it is
    the one that recurs.
    """
    detail = ""
    for line in reversed((stderr or "").splitlines() + (stdout or "").splitlines()):
        line = line.strip()
        if not line or line.startswith('{"type":"system"'):
            continue
        if line.startswith("{"):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            text = event.get("result") or event.get("error") or ""
            if text:
                detail = str(text)[:300]
                break
            continue
        detail = line[:300]
        break

    haystack = f"{stderr}\n{stdout}".lower()
    if any(marker in haystack for marker in _AUTH_MARKERS):
        return (f"claude CLI is not authenticated (exit {returncode}). Headless runs cannot "
                f"complete an OAuth flow — run `claude` interactively, or /login, then retry. "
                + (f"Detail: {detail}" if detail else ""))
    return f"claude CLI exited {returncode}: {detail or '(no diagnostic output)'}"


def preflight_llm(claude_binary: Optional[str] = None, timeout_seconds: int = 60) -> None:
    """Fail BEFORE the alert card is posted if the model cannot be reached.

    Without this, an expired login posts the alert to Slack and then dies —
    leaving an alert in the channel with no investigation under it, which in a
    demo looks exactly like the agent is broken.
    """
    import shutil

    binary = claude_binary or os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not binary:
        raise RuntimeError("`claude` CLI not found — set CLAUDE_BIN or install Claude Code")
    proc = subprocess.run(
        [binary, "-p", "reply with OK", "--allowed-tools", "", "--model",
         os.environ.get("CLAUDE_CLI_MODEL", "opus"), "--effort", "low"],
        capture_output=True, text=True, timeout=timeout_seconds,
        cwd=tempfile.gettempdir(), stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        raise RuntimeError(_diagnose_cli_failure(proc.returncode, proc.stdout, proc.stderr))


def _extract_stream(stdout: str) -> tuple:
    """(structured_output, tool_result_text, tools_used, result_meta) from stream-json.

    The tool_result corpus is what makes the no-fabrication check possible:
    it is the exact text the model was shown, so any URL not in it was made up.
    """
    structured = None
    corpus: List[str] = []
    tools: List[str] = []
    meta: dict = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue                    # CLI warnings are not protocol
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    tools.append(block.get("name", "?"))
        elif kind == "user":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") != "tool_result":
                    continue
                content = block.get("content")
                if isinstance(content, str):
                    corpus.append(content)
                elif isinstance(content, list):
                    corpus.extend(c.get("text", "") for c in content if isinstance(c, dict))
        elif kind == "result":
            meta = event
            structured = event.get("structured_output") or structured
    return structured, "\n".join(corpus), tools, meta


def _normalize_url(url: str) -> str:
    """Trim the punctuation prose leaves on a URL, and a trailing slash.

    The backtick matters as much as the full stop: BACKTICK_VALUES tells every
    agent to wrap concrete values in backticks, and a probed endpoint URL IS a
    value, so `https://jenkins.veritone.com` is the normal way for it to be
    written. Without stripping it the trailing backtick became part of the URL,
    the exact-match check failed, and a complete EndpointDown investigation was
    thrown away as a fabricated link (real failure, 2026-08-31). Asterisk and
    underscore go too — Slack's other mrkdwn wrappers.
    """
    return url.strip("`*_").rstrip(".,;:!?)]}>|\"'`*_").rstrip("/")


def _allowed_urls(alert: ParsedAlert, corpus: str, past_cases: Optional[List[dict]] = None,
                  memory_records: Optional[List[dict]] = None) -> Set[str]:
    """Every URL the model was legitimately shown."""
    allowed = set(_URL.findall(alert.raw_text)) | set(_URL.findall(corpus))
    if alert.incident_url:
        allowed.add(alert.incident_url)
    # Case-library URLs (source_permalink, confirmed_links, or any real link
    # embedded in fix_reference) are real — captured from an actual tool call
    # when the case was built, see each agent's data/cases.json's _note — so
    # citing one is not fabrication. Without this, a matching
    # known_root_causes entry's own link would be rejected exactly like an
    # invented one.
    for case in past_cases or []:
        for cause in case.get("known_root_causes") or []:
            allowed |= set(_URL.findall(json.dumps(cause)))
    # Memory records are this agent's own PAST validated output — any URL in
    # one already passed this exact check when it was first produced, so
    # re-citing it now (e.g. the same confirmed_links PR link) is not a new
    # fabrication either.
    for record in memory_records or []:
        allowed |= set(_URL.findall(json.dumps(record)))
    return {_normalize_url(u) for u in allowed}


# A Slack mention/link only works as `<@U123>` / `<url|text>`. Models sometimes
# emit the HTML-escaped form (`&lt;@devops-oncall&gt;`) — seen for real on a
# 2026-08-27 Kubernetes run — and Slack renders that as literal text, silently
# dropping the ping. This is a RENDERING repair, deliberately narrow: it only
# rewrites entity sequences that form a mention/link delimiter, and it never
# touches a URL, an evidence key or any other claim (those still abort loudly
# per CLAUDE.md non-negotiable #2).
_ESCAPED_DELIMITER = re.compile(r"&lt;((?:@|#|!|https?:)[^&]{1,200}?)&gt;")


def _unescape_slack_delimiters(text: str) -> str:
    return _ESCAPED_DELIMITER.sub(lambda m: f"<{m.group(1).replace('&amp;', '&')}>", text)


def _validate(output: dict, alert: ParsedAlert, corpus: str,
              evidence_keys: List[str], past_cases: Optional[List[dict]] = None,
              memory_records: Optional[List[dict]] = None) -> tuple:
    allowed_keys = set(evidence_keys)
    posts: List[PlannedPost] = []
    for raw_post in output.get("posts", []):
        keys = raw_post.get("evidence_keys") or []
        bad = [k for k in keys if k not in allowed_keys]
        if bad:
            raise RuntimeError(f"Investigation referenced evidence that was never captured: {bad}")
        text = raw_post.get("text", "")
        repaired = _unescape_slack_delimiters(text)
        if repaired != text:
            print(f"note: un-escaped a Slack mention/link delimiter in a post "
                  f"({text[:60]!r}) — the escaped form posts as literal text and drops "
                  f"the ping", file=sys.stderr)
        posts.append(PlannedPost(text=repaired, evidence_keys=keys))

    # Exact match only. Prefix matching would be a hole rather than a
    # convenience: with "https://veritone.atlassian.net/" anywhere in the
    # corpus, a startswith() check authorizes ".../browse/VE-99999" — a
    # fabricated ticket link wearing a real host.
    allowed_urls = _allowed_urls(alert, corpus, past_cases, memory_records)
    for post in posts:
        for raw_url in _URL.findall(post.text):
            url = _normalize_url(raw_url)
            if url not in allowed_urls:
                raise RuntimeError(
                    f"Investigation emitted a URL it never read: {raw_url!r} — refusing to post. "
                    f"({len(allowed_urls)} URLs were available from the alert and tool results.)"
                )

    # A ts the tools never returned is the same failure mode as a made-up URL:
    # it would send a reader to a thread that does not exist.
    prior = [ts for ts in (output.get("prior_incidents") or []) if ts]
    unknown = [ts for ts in prior if ts not in corpus]
    if unknown:
        raise RuntimeError(f"Investigation cited message timestamps it never read: {unknown}")

    # Compare after the same normalization, so a mention that IS in a post but
    # escaped differently no longer throws the whole investigation away. The
    # guardrail itself is unchanged: a mention in no post is still a hard error,
    # because the mention only ever reaches Slack through a post.
    mention = _unescape_slack_delimiters(output.get("owning_team_mention", "") or "").strip()
    if mention and not any(mention in p.text for p in posts):
        raise RuntimeError(
            f"owning_team_mention {mention!r} was decided but appears in no post — the "
            "mention is only ever posted via the posts array, so this would drop the escalation"
        )

    decision = Decision(
        should_post=bool(output.get("should_post", False)),
        reasoning=output.get("reasoning", ""),
        root_cause_narrative=output.get("root_cause_narrative", ""),
        owning_team_mention=mention,
        posts=posts,
        proposed_action=_clean_action(output.get("proposed_action")),
    )
    return decision, prior


def _clean_action(raw) -> Optional[dict]:
    """Normalize proposed_action, or None when there is nothing to propose."""
    if not isinstance(raw, dict):
        return None
    summary = (raw.get("summary") or "").strip()
    if not summary:
        return None
    return {"summary": summary,
            "command": (raw.get("command") or "").strip(),
            "risk": (raw.get("risk") or "").strip()}


def investigate_alert(
    alert: ParsedAlert,
    evidence_keys: Optional[List[str]] = None,
    evidence_descriptions: Optional[Dict[str, str]] = None,
    panel_hint: str = "",
    past_cases: Optional[List[dict]] = None,
    mcp_config: Path = DEFAULT_MCP_CONFIG,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
    claude_binary: Optional[str] = None,
) -> Investigation:
    """Run one tool-using investigation and return a validated Decision."""
    import shutil

    evidence_keys = list(evidence_keys or [])
    binary = claude_binary or os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not binary:
        raise RuntimeError("`claude` CLI not found — set CLAUDE_BIN or install Claude Code")
    if not Path(mcp_config).exists():
        raise RuntimeError(f"MCP config not found: {mcp_config}")
    resolved_config = _resolve_mcp_config(Path(mcp_config))

    # Per-run directory, so concurrent investigations cannot read or clobber
    # each other's rendered panels.
    run_dir = _run_evidence_dir(alert)
    grafana_mcp.clear_manifest(run_dir)

    prompt = build_prompt(alert, evidence_keys, evidence_descriptions, past_cases)
    if panel_hint:
        prompt += f"\n\nHINT (not a substitute for checking): {panel_hint}"

    cmd = [
        binary, "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--system-prompt", SYSTEM_PROMPT,
        # --strict-mcp-config: ONLY our server. Without it the CLI would also
        # load whatever MCP servers the invoking user happens to have
        # configured, which is a different (and unaudited) tool surface.
        "--mcp-config", str(resolved_config), "--strict-mcp-config",
        "--allowed-tools", *ALL_TOOLS,
        "--json-schema", json.dumps(_schema(evidence_keys)),
        "--model", model or os.environ.get("CLAUDE_CLI_MODEL", "opus"),
        "--effort", effort or os.environ.get("CLAUDE_CLI_EFFORT", "medium"),
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True,
        timeout=timeout_seconds or int(os.environ.get("INVESTIGATE_TIMEOUT", "900")),
        # Run from the repo so .mcp.json's relative cwd resolves, but the
        # system prompt is the whole brief — CLAUDE.md must not ride along.
        cwd=str(_REPO_ROOT),
        stdin=subprocess.DEVNULL,
        # The Grafana MCP server runs as a subprocess of the CLI and reads
        # EVIDENCE_DIR at import, so this is what pins its renders to this run.
        env={**os.environ, "CLAUDE_DISABLE_PROJECT_CONTEXT": "1",
             "EVIDENCE_DIR": str(run_dir)},
    )
    if proc.returncode != 0:
        raise RuntimeError(_diagnose_cli_failure(proc.returncode, proc.stdout, proc.stderr))

    structured, corpus, tools, meta = _extract_stream(proc.stdout)
    if meta.get("is_error"):
        raise RuntimeError(f"claude CLI reported an error: {str(meta.get('result'))[:400]}")
    if structured is None:
        raise RuntimeError(f"No structured output returned. Tail: {proc.stdout[-500:]}")

    # Whatever the model actually rendered is now valid evidence, on top of
    # anything the caller supplied. A key it did NOT render still fails.
    rendered = grafana_mcp.load_manifest(run_dir)
    evidence_files = {k: Path(v["path"]) for k, v in rendered.items()}
    decision, prior = _validate(structured, alert, corpus,
                                evidence_keys + list(rendered), past_cases)
    return Investigation(
        decision=decision,
        prior_incidents=prior,
        tools_used=tools,
        sources_seen=len(corpus.split("---")) if corpus else 0,
        cost_usd=float(meta.get("total_cost_usd") or 0.0),
        duration_ms=int(meta.get("duration_api_ms") or 0),
        evidence_files=evidence_files,
    )
