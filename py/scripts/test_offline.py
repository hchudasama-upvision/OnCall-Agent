#!/usr/bin/env python3
"""
Everything that can be tested with no credentials, no network, no Slack.

    python py/scripts/test_offline.py

Run this after touching the parser, the guardrails, the panel map or the
Slack block shaping. It is fast (~2s), it hits no API, and it is the only
test that can run in CI or on a laptop with an empty .env.

What it covers, and why each one exists:
  parser      — the real captured cards in fixtures/alerts-devops-real.json.
                Every assertion here is a bug that shipped once: the title
                read as "119881", lower-case metadata keys dropped, " : "
                not split, "NOC Health Check - Systems Alerting" truncated.
  guardrails  — the checks that stop a fabricated link, a fabricated thread
                ts, or an uncaptured screenshot from reaching Slack.
  routing     — trigger/suppress/dedupe decisions.
  formatting  — the #comms-noc header, and Slack's two size caps.
  mcp         — the MCP server answers the protocol handshake.

What it does NOT cover: anything needing Slack, Grafana, Edge or the model.
Use `python py/scripts/check_setup.py` for those.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import bootstrap  # noqa: E402  — must precede third-party imports

bootstrap()

_ROOT = Path(__file__).resolve().parents[2]


def _test_config(**overrides):
    """A config that never points at the real NOC channels."""
    from oncall_agent.config import load_config
    env = {"SLACK_BOT_TOKEN": "x", "ALERTS_CHANNEL": "C0TESTALERTS",
           "SLACK_CHANNEL": "C0TESTCOMMS"}
    env.update(overrides)
    return load_config(env)


_passed = 0
_failed: list = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global _passed
    if condition:
        _passed += 1
    else:
        _failed.append(f"{name}{' — ' + detail if detail else ''}")
        print(f"  FAIL  {name}" + (f"\n        {detail}" if detail else ""))


def group(title: str) -> None:
    print(f"\n{title}")


# ------------------------------------------------------------------- parser

def test_parser() -> None:
    from oncall_agent.alert_parser import (
        is_engine_failure_rate,
        parse_alert_message,
        to_victorops_incident,
    )

    group("parser (real captured #alerts-devops cards)")
    fixtures = json.loads((_ROOT / "fixtures" / "alerts-devops-real.json").read_text())
    # Keyed by the FULL _case string and selected by substring: two fixtures
    # both begin "victorops incident", so truncating the key silently drops
    # one of them and every assertion then runs against the wrong card.
    parsed = {m["_case"]: parse_alert_message(m, channel_id="C909ZH4ET")
              for m in fixtures["messages"]}

    def fixture(needle: str):
        matches = [a for case, a in parsed.items() if needle in case]
        if len(matches) != 1:
            raise AssertionError(f"{needle!r} matched {len(matches)} fixtures, expected exactly 1")
        return matches[0]

    engine = fixture("engine failure rate")
    check("title comes from the heading, not INCIDENT_NAME",
          engine.incident_name == "[FIRING:1] aiw-prd5001 : Engine failure rate above 15%",
          f"got {engine.incident_name!r} (a regression here yields threads headed 'Alert: 119881')")
    check("incident number parsed", engine.incident_number == 119881)
    check("VictorOps portal url captured",
          engine.incident_url == "https://portal.victorops.com/ui/wazee-digital-inc/incident/119881")
    check("' : ' separator splits the alertname",
          engine.alert_name == "Engine failure rate above 15%", f"got {engine.alert_name!r}")
    check("lower-case metadata keys are read",
          engine.monitoring_tool == "Alertmanager", f"got {engine.monitoring_tool!r}")
    check("environment extracted", engine.environment_key == "aiw-prd5001")
    check("escalation policy from CONTACTGROUPNAME", engine.escalation_policy == "devops-oncall")
    check("engine-failure route detected", is_engine_failure_rate(engine))
    check("RESOLVED incident is not a trigger",
          engine.is_resolved and not engine.is_trigger,
          "auto-resolved incidents must not open a thread")

    alb = fixture("plain trigger case")
    check("firing, unsuppressed incident is a trigger",
          alb.is_trigger and alb.alert_phase == "FIRING" and alb.incident_number == 119880)
    check("' : ' then ' - ' both peeled off the alertname",
          alb.alert_name == "ALBUnhealthyHostCritical", f"got {alb.alert_name!r}")

    noc = fixture("still firing")
    check("hyphenated alertname is not truncated",
          noc.alert_name == "NOC Health Check - Systems Alerting",
          f"got {noc.alert_name!r} (rsplit on ' - ' would give 'Systems Alerting')")
    check("ACKED incident is still parsed as firing-phase ACKED", noc.alert_phase == "ACKED")

    check("NOC health check is suppressed by the shipped config",
          _test_config().is_suppressed(noc.fingerprint),
          "the NOC does not want the agent triaging its own monitoring bot")

    ack = fixture("thread reply")
    check("ACK reply is an update, never a trigger",
          ack.kind == "incident_update" and not ack.is_trigger)

    alertmanager = fixture("segments")
    check("multiple ' - ' segments: name is the last",
          alertmanager.alert_name == "AlbUnhealthyHostWarning",
          f"got {alertmanager.alert_name!r}")
    check("raw Alertmanager post is not a trigger", not alertmanager.is_trigger)

    pando = fixture("two parenthesised groups")
    check("PandoLogic classified by its local alertmanager host",
          pando.source == "pandologic", f"got {pando.source!r}")
    check("two parenthesised groups both stripped",
          pando.alert_name == "SQL41 DiskSpaceUtilizationWarning",
          f"got {pando.alert_name!r}")

    chronic = fixture("chronic staging noise")
    check("FIRING:45 count parsed", chronic.firing_count == 45)
    check("fingerprint is stable for suppression",
          chronic.fingerprint == "kubepodcrashlooping|aiw-stg198", f"got {chronic.fingerprint!r}")

    incident = to_victorops_incident(engine)
    check("portal url flows into the posted header",
          incident.slack_permalink == engine.incident_url)


# --------------------------------------------------------------- guardrails

def test_guardrails() -> None:
    from oncall_agent.investigate import _validate
    from oncall_agent.types import ParsedAlert

    group("guardrails (no fabricated links, ts, or evidence)")
    alert = ParsedAlert(
        source="victorops", kind="incident", raw_text="incident text",
        incident_url="https://portal.victorops.com/ui/wazee-digital-inc/incident/119884",
    )
    corpus = ("[ts=1787148500.858899 from=U066X9SKBQ9]\n"
              "Known issue https://veritone.atlassian.net/browse/VE-23618")

    def decision(text="ok", keys=None, prior=None, mention=""):
        return {"should_post": True, "reasoning": "", "prior_incidents": prior or [],
                "root_cause_narrative": "", "owning_team_mention": mention,
                "posts": [{"text": text, "evidence_keys": keys or []}]}

    def accepts(payload, evidence_keys=None) -> bool:
        try:
            _validate(payload, alert, corpus, evidence_keys=evidence_keys or [])
            return True
        except RuntimeError:
            return False

    # BACKTICK_VALUES tells agents to wrap values in backticks, and a probed
    # endpoint URL is a value. The trailing backtick used to be read as part of
    # the URL, so a real EndpointDown run was discarded as a fabricated link.
    check("a backticked URL is not mistaken for a fabricated one",
          accepts(decision("endpoint `https://veritone.atlassian.net/browse/VE-23618` is down")),
          "a URL in backticks must normalize to the same URL")
    check("a genuinely invented URL is still refused",
          not accepts(decision("see `https://veritone.atlassian.net/browse/VE-99999`")))

    check("url that was actually read is allowed",
          accepts(decision("see https://veritone.atlassian.net/browse/VE-23618")))
    check("trailing punctuation does not break a real url",
          accepts(decision("see https://veritone.atlassian.net/browse/VE-23618.")))
    check("the incident's own url is allowed",
          accepts(decision("see https://portal.victorops.com/ui/wazee-digital-inc/incident/119884")))
    check("fabricated ticket url REJECTED",
          not accepts(decision("see https://veritone.atlassian.net/browse/VE-99999")))
    check("path extension off a real host REJECTED",
          not accepts(decision("see https://veritone.atlassian.net/browse/VE-23618/evil")),
          "a real host must never authorize an invented path under it")
    check("fabricated slack permalink REJECTED",
          not accepts(decision("see https://veritone.slack.com/archives/C01F810QM96/p1700000000000000")))
    check("real prior ts accepted", accepts(decision(prior=["1787148500.858899"])))
    check("invented prior ts REJECTED", not accepts(decision(prior=["1700000000.000001"])))
    check("uncaptured evidence key REJECTED", not accepts(decision(keys=["screenshot_tasks"])))
    check("captured evidence key accepted",
          accepts(decision(keys=["screenshot_tasks"]), evidence_keys=["screenshot_tasks"]))
    check("escalation decided but never posted REJECTED",
          not accepts(decision("no mention here", mention="<@U031E6JJPFU>")),
          "would silently drop the escalation")
    check("escalation present in a reply accepted",
          accepts(decision("ping <@U031E6JJPFU> FYI^^", mention="<@U031E6JJPFU>")))


# ------------------------------------------------------------------ routing

def test_routing() -> None:
    import re
    import tempfile

    from oncall_agent.alert_parser import parse_alert_message
    from oncall_agent.config import load_config
    from oncall_agent.handler import SeenStore, should_handle

    group("routing (trigger / suppress / dedupe)")
    state = tempfile.mkdtemp()
    config = load_config({"SLACK_BOT_TOKEN": "x", "STATE_DIR": state,
                          "ALERTS_CHANNEL": "C0TESTALERTS", "SLACK_CHANNEL": "C0TESTCOMMS"})
    seen = SeenStore(Path(state) / "seen.json", 60)

    fixtures = json.loads((_ROOT / "fixtures" / "alerts-devops-real.json").read_text())
    # A FIRING, unsuppressed incident — "still firing" is the NOC health check,
    # which is on the suppression list and must NOT be the positive case here.
    firing = parse_alert_message(next(
        m for m in fixtures["messages"] if "the plain trigger case" in m["_case"]))
    chronic = parse_alert_message(next(
        m for m in fixtures["messages"] if "chronic" in m["_case"]))

    check("default POST_MODE is dry_run", config.post_mode == "dry_run")
    check("default TRIGGER_ON is victorops", config.trigger_on == "victorops")
    check("default PROFILE is test", config.profile == "test")

    from oncall_agent.config import PROD_ALERTS_CHANNEL, load_config
    check("test profile never falls back to the production channels",
          config.alerts_channel != PROD_ALERTS_CHANNEL)
    try:
        load_config({"SLACK_BOT_TOKEN": "x"})
        refused = False
    except SystemExit:
        refused = True
    check("test profile REFUSES to start with no channels set", refused,
          "an unset ALERTS_CHANNEL must not silently mean production")
    check("production profile opts in to the real channels",
          load_config({"SLACK_BOT_TOKEN": "x", "PROFILE": "production"}).alerts_channel
          == PROD_ALERTS_CHANNEL)
    check("firing VictorOps incident is handled", should_handle(firing, config, seen)[0])
    check("raw Alertmanager warning is skipped", not should_handle(chronic, config, seen)[0])

    config.trigger_on = "all"
    check("TRIGGER_ON=all picks up warnings", should_handle(chronic, config, seen)[0])
    config.suppressed = [re.compile(r"kubepodcrashlooping\|aiw-stg198", re.IGNORECASE)]
    check("suppression list blocks chronic noise", not should_handle(chronic, config, seen)[0])
    config.suppressed = []

    seen.record(firing.fingerprint)
    check("same fingerprint inside the window is a duplicate",
          not should_handle(firing, config, seen)[0])
    check("dedupe state persists to disk", (Path(state) / "seen.json").exists())

    # Single-channel: the mode the test workspace runs in, and the one that
    # makes self-loop protection load-bearing.
    one = _test_config(ALERTS_CHANNEL="C0SAME", SLACK_CHANNEL="C0SAME")
    check("same alerts/comms channel is detected", one.single_channel)
    check("different channels is not single-channel", not config.single_channel)

    ours = parse_alert_message({"ts": "9", "bot_id": "B0SELF", "attachments": [{"text":
        "*<https://portal.victorops.com/ui/o/incident/9|Incident #9>: "
        "[FIRING:1] uk-1 : uk-prod - ALBUnhealthyHostCritical*\n"
        "```monitoring_tool: Alertmanager\nCURRENT_ALERT_PHASE: FIRING\n```"}]})
    fresh = SeenStore(Path(tempfile.mkdtemp()) / "s.json", 60)
    check("an alert-shaped message WE posted is refused",
          not should_handle(ours, one, fresh, own_ids={"B0SELF"})[0],
          "reading and writing one channel must not loop")
    check("the same message from anyone else is handled",
          should_handle(ours, one, fresh, own_ids={"B0OTHER"})[0])

    check("no user token -> prefetch", config.investigation_mode(True) == "prefetch")
    config.slack_user_token = "xoxp-test"
    check("user token -> tools", config.investigation_mode(True) == "tools")
    check("no mcp config -> prefetch even with a user token",
          config.investigation_mode(False) == "prefetch")


def test_specialist_routing() -> None:
    from oncall_agent.Agents.MASTER_Agent.router import route_alert
    from oncall_agent.types import ParsedAlert

    group("specialist routing (domain-expert dispatch)")

    def mk(alert_name: str) -> ParsedAlert:
        return ParsedAlert(source="victorops", kind="incident", raw_text=alert_name,
                           alert_name=alert_name, incident_name=alert_name)

    cases = [
        ("Engine failure rate above 15%", "edge_ui"),
        # Backlog moved from grafana_metrics to edge_ui (owner, 2026-08-31): the
        # graphs are in Edge UI, not Grafana, and it is the same alert family.
        ("Engine backlog critical for 30m", "edge_ui"),
        ("Engine failure rate 100% for all engine in every Environment", "edge_ui"),
        ("KubePodsNotReady", "kubernetes"),
        ("KubePodCrashLooping", "kubernetes"),
        ("KubePersistentVolumeFillingUp", "kubernetes"),
        ("Track Job", "runscope"),
        ("Site DNS Valdation", "runscope"),
        ("DiskSpaceUtilizationCritical", "grafana_metrics"),
        ("VM Disk Usage above 90% - Prometheus", "grafana_metrics"),
        # Moved to the AWS agent 2026-08-31 (owner): the evidence is ELB target
        # health, not a Grafana panel.
        ("AlbUnhealthyHostCritical", "aws"),
        ("Some totally unrecognized alert type", None),
    ]
    for alert_name, expected in cases:
        got = route_alert(mk(alert_name))
        check(f"{alert_name!r} routes to {expected}", got == expected, f"got {got!r}")

    from oncall_agent.Agents.registry import SPECIALISTS
    for name in ("edge_ui", "kubernetes", "grafana_metrics", "runscope"):
        check(f"specialist {name!r} is registered with tools + a system prompt",
              name in SPECIALISTS and SPECIALISTS[name]["tools"] and SPECIALISTS[name]["system_prompt"])
    check("every specialist gets the Slack tools too (universal history grounding)",
          all(any(t.startswith("mcp__noc_slack__") for t in cfg["tools"])
              for cfg in SPECIALISTS.values()))
    check("edge_ui specialist has no tools from other domains",
          not any("noc_grafana" in t or "noc_runscope" in t for t in SPECIALISTS["edge_ui"]["tools"]))
    check("edge_ui specialist has the GitHub tools (the one domain where a real code/PR check pays off)",
          any(t.startswith("mcp__noc_github__") for t in SPECIALISTS["edge_ui"]["tools"]))
    for other in ("kubernetes", "grafana_metrics", "runscope"):
        check(f"{other!r} specialist has NO GitHub tools (kept edge_ui-only for now)",
              not any(t.startswith("mcp__noc_github__") for t in SPECIALISTS[other]["tools"]))
    # Owner's direction (2026-08-26): confirmed data only, and keep the thread
    # short. Both rules live in Agents/shared_prompt.py so no agent can be given a
    # looser standard than its siblings — including the generalist fallback paths.
    from oncall_agent import investigate as investigate_prompts

    _every_prompt = ([(name, cfg["system_prompt"]) for name, cfg in SPECIALISTS.items()]
                     + [("generalist(tools)", investigate_prompts.SYSTEM_PROMPT),
                        ("generalist(no-tools)", investigate_prompts.CONTEXT_PROMPT)])
    for name, text in _every_prompt:
        check(f"{name!r} is told to post confirmed data only, no guesses",
              "CONFIRMED DATA ONLY" in text)
        # edge_ui has a validated fixed 7-post structure, so it carries the same
        # discipline scoped to that structure instead of the generic reply cap.
        check(f"{name!r} is told to keep the thread short and on point",
              "KEEP IT SHORT" in text or "These seven ARE the thread" in text)
        check(f"{name!r} is told not to recommend — observations only",
              "OBSERVATIONS ONLY" in text or "These seven ARE the thread" in text)
        # Values in backticks scan far better in Slack; the same block also warns
        # that backticking an @mention or a link stops Slack rendering it.
        check(f"{name!r} is told to backtick concrete values",
              "BACKTICK EVERY VALUE" in text)

    # ...and the lock behind those prompts: code decides what reaches Slack, so a
    # model that fills proposed_action anyway still cannot post an action request.
    from oncall_agent import slack_post
    from oncall_agent.types import Decision, PlannedPost, VictorOpsIncident

    check("proposed_action posting is gated off", slack_post.POST_PROPOSED_ACTION is False)
    _printed: list = []
    slack_post.DryRunSlackPoster(log=_printed.append).post_decided_thread(
        VictorOpsIncident(incident_number=1, organization="o", incident_name="x",
                          entity_display_name="", monitoring_tool="", state_message="",
                          escalation_policy="", slack_permalink="", created_at=""),
        Decision(should_post=True, reasoning="", root_cause_narrative="",
                 owning_team_mention="",
                 posts=[PlannedPost(text="observed: x", evidence_keys=[])],
                 proposed_action={"summary": "restart the thing", "command": "", "risk": ""}),
        {})
    _log = "\n".join(_printed)
    check("a model-emitted proposed_action is suppressed, not posted",
          "would ask for approval" not in _log and "suppressed" in _log, _log[-200:])

    check("every specialist gets the Jira tools too (a related ticket isn't domain-specific)",
          all(any(t.startswith("mcp__noc_jira__") for t in cfg["tools"])
              for cfg in SPECIALISTS.values()))
    for name in ("edge_ui", "kubernetes", "grafana_metrics", "runscope"):
        check(f"specialist {name!r} has its own case_library",
              "case_library" in SPECIALISTS[name] and SPECIALISTS[name]["case_library"] is not None)

    # --- agent-wise memory round trip (write, read back, then clean up) ---
    from oncall_agent import agent_memory
    from oncall_agent.types import Decision

    _MEM_TEST_FP = "__test_offline_fingerprint__"
    _mem_path = agent_memory._memory_path("edge_ui", _MEM_TEST_FP)
    _mem_path.unlink(missing_ok=True)
    try:
        check("no memory yet for a fresh fingerprint",
              agent_memory.recall("edge_ui", _MEM_TEST_FP) == [])
        fake_alert = mk("Engine failure rate above 15%")
        fake_decision = Decision(should_post=True, reasoning="test", root_cause_narrative="test cause",
                                 owning_team_mention="@team", posts=[])
        agent_memory.remember("edge_ui", _MEM_TEST_FP,
                              agent_memory.build_record(fake_alert, fake_decision, _MEM_TEST_FP))
        recalled = agent_memory.recall("edge_ui", _MEM_TEST_FP)
        check("memory round-trips: one record written is one record recalled",
              len(recalled) == 1 and recalled[0]["root_cause_narrative"] == "test cause")
        check("format_memory renders written records, not the empty-case message",
              "test cause" in agent_memory.format_memory(recalled))
        check("format_memory labels a genuinely empty history as empty",
              "no memory yet" in agent_memory.format_memory([]))
    finally:
        _mem_path.unlink(missing_ok=True)


# --------------------------------------------------------------- formatting

def test_formatting() -> None:
    from oncall_agent.alert_parser import parse_alert_message, to_victorops_incident
    from oncall_agent.slack_blocks import SECTION_LIMIT, TEXT_LIMIT, fallback_text, sections
    from oncall_agent.evidence_panels import PanelMap, load_panel_map

    group("formatting, panel map, case library")
    fixtures = json.loads((_ROOT / "fixtures" / "alerts-devops-real.json").read_text())
    alert = parse_alert_message(fixtures["messages"][0])
    from oncall_agent.slack_post import compose_top_level_text

    header = compose_top_level_text(to_victorops_incident(alert))
    check("header matches the real #comms-noc convention",
          header == ("Alert:\n> *<https://portal.victorops.com/ui/wazee-digital-inc/incident/119881"
                     "|Incident #119881>: [FIRING:1] aiw-prd5001 : Engine failure rate above 15%*"),
          f"got {header!r}")

    long_text = "\n\n".join(["paragraph " + "x" * 500 for _ in range(30)])
    chunks = sections(long_text)
    check("every section chunk is under Slack's 3000-char block cap",
          all(len(c) <= SECTION_LIMIT for c in chunks), f"max {max(len(c) for c in chunks)}")
    check("fallback text is under Slack's separate 4000-char text cap",
          len(fallback_text(long_text)) <= TEXT_LIMIT + 10)
    check("one unbroken paragraph is still split",
          all(len(c) <= SECTION_LIMIT for c in sections("y" * 9000)))

    panel_map = PanelMap({
        "dashboard_uid": "top",
        "panels_by_alertname": {
            "KubePodsNotReady / KubePodCrashLooping": [8, 9],
            "Engine failure rate above 15%": {
                "dashboard_uid": "own", "panels": [1],
                "from": "now-3h", "variables": {"var-env": "{env}"}},
        },
    })
    check("compound panel-map key matches each half",
          [s.panel_id for s in panel_map.specs_for("KubePodCrashLooping")] == [8, 9])
    spec = panel_map.specs_for("Engine failure rate above 15%", "aiw-prd5001")[0]
    check("{env} substituted into template variables",
          spec.variables == {"var-env": "aiw-prd5001"} and spec.dashboard_uid == "own")
    check("unmapped alert yields no panels", panel_map.specs_for("Nothing") == [])
    check("shipped panel map loads", load_panel_map() is not None)

    from oncall_agent.handler import CASES

    check("merged case library loaded", len(CASES.cases) > 0, f"{len(CASES.cases)} cases")
    check("case fingerprinting works",
          CASES.fingerprint("[FIRING:2] aiw-prd5001 - KubePersistentVolumeFillingUp (x)")
          == "KubePersistentVolumeFillingUp")

    from oncall_agent.Agents import registry as specialists_reg

    check("every specialist's own case library is non-empty",
          all(cfg["case_library"].cases for cfg in specialists_reg.SPECIALISTS.values()),
          {k: len(v["case_library"].cases) for k, v in specialists_reg.SPECIALISTS.items()})
    check("merged CASES equals the sum of every specialist's own cases",
          len(CASES.cases) == sum(len(cfg["case_library"].cases)
                                  for cfg in specialists_reg.SPECIALISTS.values()))

    # PandoLogic renders the host INTO the alertname, so the parsed alert name is
    # "SVC120 DiskSpaceUtilizationWarning". Before the suffix match this returned
    # no cases at all for the whole family — silently, on a real dry run.
    grafana_cases = specialists_reg.SPECIALISTS["grafana_metrics"]["case_library"]
    hosty = grafana_cases.find("SVC120 DiskSpaceUtilizationWarning")
    # Two names for one case that differ only in letter case must not double it.
    aws_cases = specialists_reg.SPECIALISTS["aws"]["case_library"]
    alb = aws_cases.find("ALBUnhealthyHostCritical")
    check("a case is not returned twice when its names differ only in case",
          len(alb) == len({id(c) for c in alb}), [c["fingerprint"] for c in alb])

    check("host-prefixed alertname still finds its case (suffix match)",
          [c["fingerprint"] for c in hosty] == ["DiskSpaceUtilization/pandologic-windows"],
          [c["fingerprint"] for c in hosty])
    check("suffix match cannot satisfy Critical with a Warning-only entry",
          all("Warning" not in c["alertname"] or "Critical" in c["alertname"]
              for c in grafana_cases.find("ny1wv9601 DiskSpaceUtilizationCritical")),
          [c["fingerprint"] for c in grafana_cases.find("ny1wv9601 DiskSpaceUtilizationCritical")])
    check("an unrelated trailing word does not suffix-match a case",
          grafana_cases.find("SomethingElse Entirely") == [])

    # The graph-answered alert families each carry a verified dashboard block —
    # that is the knowledge panel_map used to hold as static config.
    graph_cases = {c["fingerprint"]: c for c in grafana_cases.cases if c.get("dashboard")}
    check("every graph-alert family has a dashboard block with a uid",
          {"KubePersistentVolumeFillingUp/*", "DiskSpaceUtilization/pandologic-windows",
           "VmActiveRedAlarms/pandologic-vmware", "HighMemoryUtilization/windows"}
          <= set(graph_cases) and all(c["dashboard"].get("uid") for c in graph_cases.values()),
          sorted(graph_cases))
    check("the two PVC copies point at each other (they must be edited together)",
          all(any("_shared_with" in c and "KubePersistentVolumeFillingUp" in c["fingerprint"]
                  for c in cfg["case_library"].cases)
              for name, cfg in specialists_reg.SPECIALISTS.items()
              if name in ("kubernetes", "grafana_metrics")))


def test_api_health_pairing() -> None:
    """Panel pairing for response-code alerts, against the real titles from
    'API Services - Overview' (read live 2026-08-28). No network: the dashboard
    payload is stubbed, because what is being tested is the pairing RULE."""
    from oncall_agent.Agents.Grafana_Agent import api_health

    group("api health panel pairing (stubbed dashboard)")

    titles = {
        16: "US-Prod Response Codes - haproxy", 12: "Prod - API Success Rate",
        25: "US-Prod Response Codes - nginx -ai13s", 26: "US-Prod API Success Rate -ai13s",
        15: "UK-Prod Response Codes - nginx", 14: "UK-Prod API Success Rate",
        9: "DMH CrUX - API Response Codes", 10: "DMH CrUX - API Success Rate",
        23: "DMH Core - API Response Codes", 24: "DMH Core - API Success Rate",
        27: "Azure Prod Response Codes - nginx -ai13s",
        28: "Azure-prod API Success Rate -ai13s",
        29: "Azure Stage Response Codes - nginx -ai13s",
        30: "Azure-stage API Success Rate -ai13s",
    }
    stub = {"dashboard": {"panels": [{"id": pid, "title": title} for pid, title in titles.items()],
                          "templating": {"list": []}}}
    original = api_health._dashboard
    api_health._dashboard = lambda: stub
    try:
        expected = [
            ("US-Prod Response Codes - nginx -ai13s   aiWARE/prod", 25, 26),
            # The bug this rule exists for: haproxy must NOT pair with the
            # -ai13s stat (26), and must not pair with UK's (14) either.
            ("US-Prod Response Codes - haproxy", 16, 12),
            ("UK-Prod Response Codes - nginx", 15, 14),
            ("Azure Prod Response Codes - nginx -ai13s", 27, 28),
            ("Azure Stage Response Codes - nginx -ai13s", 29, 30),
            ("DMH CrUX - API Response Codes", 9, 10),
            ("DMH Core - API Response Codes", 23, 24),
        ]
        for alert, codes, rate in expected:
            pair = api_health.find_panel_pair(alert)
            check(f"{alert[:38]!r} -> panels {codes}/{rate}",
                  pair is not None and pair.codes_panel == codes and pair.rate_panel == rate,
                  f"got {pair and (pair.codes_panel, pair.rate_panel)}")
        check("an unrelated alert matches no panel",
              api_health.find_panel_pair("KubePodsNotReady") is None)
        check("alert-family detection is narrow",
              api_health.is_api_health_alert("US-Prod Response Codes - nginx")
              and api_health.is_api_health_alert("UK-Prod API Success Rate")
              and not api_health.is_api_health_alert("KubePersistentVolumeFillingUp"))
    finally:
        api_health._dashboard = original

    # The formula, applied to the counts the real panel legend showed.
    rate = api_health.SuccessRate(ratio=1 - 30 / (1072747 + 6555 + 30),
                                 counts={"2xx": 1072747, "4xx": 6555, "5xx": 30, "neg": 0})
    check("success rate formats to 4 decimals like the panel",
          rate.percent == "99.9972%", rate.percent)
    check("99.9972% counts as recovered against the 99.99% threshold", rate.recovered)
    check("99.98% does not",
          not api_health.SuccessRate(ratio=0.9998, counts={"2xx": 1}).recovered)


def test_follow_up() -> None:
    """The timed re-check: eligibility, the posted wording, and the persistence
    that lets a restart resume it."""
    import time as _time

    from oncall_agent import follow_up
    from oncall_agent.Agents.Grafana_Agent import api_health
    from oncall_agent.types import ParsedAlert

    group("follow-up re-check")

    def alert(name: str) -> ParsedAlert:
        return ParsedAlert(source="victorops", kind="incident", raw_text=name,
                           alert_name=name, incident_name=name)

    # applies() needs Grafana configured; test the family predicate directly so
    # the result does not depend on this machine's .env.
    check("response-code alerts are the follow-up family",
          api_health.is_api_health_alert(alert("US-Prod Response Codes - nginx").alert_name))
    check("pod alerts are not",
          not api_health.is_api_health_alert(alert("KubePodCrashLooping").alert_name))

    check_obj = follow_up.PendingCheck(
        incident_number="119881", alert_name="US-Prod Response Codes - nginx -ai13s",
        thread_ts="1788168357.875587", channel="C0TEST", codes_panel=25, rate_panel=26,
        environment="US-Prod Response Codes - nginx -ai13s",
        created_at=_time.time(), due_at=_time.time() + 300, attempt=1, attempts_total=2)
    message = follow_up.compose_message(
        check_obj,
        api_health.SuccessRate(ratio=0.999972, counts={"2xx": 1072747, "5xx": 30},
                               window="now-15m -> now", panel=26))
    check("follow-up message is a field block with single-asterisk labels",
          message.startswith("*Re-check") and "*Success rate:* `99.9972%`" in message
          and "**" not in message, message)
    check("follow-up message says recovered above the threshold",
          "recovered" in message and "99.9900%" in message, message)
    below = follow_up.compose_message(
        check_obj, api_health.SuccessRate(ratio=0.9990, counts={"2xx": 1000, "5xx": 1},
                                          window="now-15m -> now", panel=26))
    check("a rate under the threshold reads as still below",
          "still below" in below and "recovered" not in below, below)
    check("follow-up posts no recommendation and no proposed action",
          not any(word in message.lower() for word in ("recommend", "should", "suggest")))

    # Persistence round trip, and the stale-record drop.
    import tempfile
    from types import SimpleNamespace
    with tempfile.TemporaryDirectory() as tmp:
        config = SimpleNamespace(state_dir=Path(tmp), evidence_dir=Path(tmp) / "ev",
                                 comms_channel="C0TEST", is_live=False)
        path = follow_up._persist(config, check_obj)
        check("a pending re-check is persisted", path.exists())
        logged: list = []
        resumed = follow_up.resume_pending(config, None, log=logged.append)
        check("a fresh record resumes", len(resumed) == 1, logged)
        stale = follow_up.PendingCheck(**{**follow_up.asdict(check_obj),
                                          "created_at": _time.time() - 9999 * 60})
        follow_up._persist(config, stale)
        follow_up._forget(config, check_obj)
        logged.clear()
        check("a stale record is dropped rather than posted late",
              follow_up.resume_pending(config, None, log=logged.append) == [],
              logged)


def test_winrm_guards() -> None:
    """The one remote-EXECUTION surface in the repo. It must be impossible to
    run anything that is not one of its own fixed read-only queries."""
    from oncall_agent.Agents.Windows_Agent import winrm_client, windows_processes

    group("winrm guards (no host contacted)")

    for bad in ("Stop-Process -Name w3wp", "Restart-Computer", "hostname",
                "top_cpu; Remove-Item C:\\", "", "Get-Process"):
        raised = False
        try:
            winrm_client.run_query("somehost", bad)
        except winrm_client.WinRmError as e:
            raised = "fixed read-only queries" in str(e) or "not a valid" in str(e)
        except Exception:                               # noqa: BLE001
            raised = False
        check(f"refuses query {bad[:26]!r}", raised)
    check("the query list is read-only shaped",
          set(winrm_client.available_queries()) ==
          {"top_cpu", "top_memory", "memory_totals", "disk_usage", "cpu_now", "uptime"},
          winrm_client.available_queries())
    for bad_host in ("-x", "host;whoami", "", "a" * 300):
        raised = False
        try:
            winrm_client.run_query(bad_host, "top_cpu")
        except winrm_client.WinRmError as e:
            raised = "not a valid hostname" in str(e) or "not set in .env" in str(e)
        check(f"refuses host {bad_host[:18]!r}", raised)

    # The idle process must never be reported as a top consumer.
    check("Idle and _Total are excluded from top lists",
          {"idle", "_total"} <= windows_processes._NOT_A_PROCESS)

    # And the credential must never appear in tool output.
    import os
    os.environ.setdefault("WINDOWS_PASSWORD", "unit-test-secret")
    answer = ""
    try:
        winrm_client.run_query("does-not-resolve.invalid", "top_cpu")
    except winrm_client.WinRmError as e:
        answer = str(e)
    check("a WinRM error never contains the password",
          os.environ["WINDOWS_PASSWORD"] not in answer, answer[:120])


def test_aws_guards() -> None:
    """The read-only AWS surface: allow-listed subcommands, validated names, and
    an RDS follow-up that refuses to guess which database."""
    from oncall_agent.Agents.AWS_Agent import aws_client

    group("aws guards (no AWS calls)")

    for service, sub in (("rds", "reboot-db-instance"), ("rds", "modify-db-instance"),
                         ("rds", "delete-db-instance"), ("ec2", "terminate-instances"),
                         ("iam", "list-users"), ("s3", "rm")):
        raised = False
        try:
            aws_client.run(service, sub, profile="main")
        except aws_client.AwsError as e:
            raised = "read-only allow-list" in str(e)
        except Exception:                               # noqa: BLE001
            raised = False
        check(f"refuses `aws {service} {sub}`", raised)

    for bad in ("--profile=other", "-x", "", "a" * 300):
        raised = False
        try:
            aws_client._validate("db instance", bad)
        except aws_client.AwsError:
            raised = True
        check(f"rejects identifier {bad[:18]!r}", raised)
    check("accepts a real instance identifier",
          aws_client._validate("db instance", "stage-core-rds2") == "stage-core-rds2")

    from oncall_agent import follow_up
    from oncall_agent.types import ParsedAlert

    def alert(name: str) -> ParsedAlert:
        return ParsedAlert(source="victorops", kind="incident", raw_text=name,
                           alert_name=name, incident_name=name)

    check("an ALB alert gets the alb_health re-check on a 3/6-minute cadence",
          follow_up.kind_for(alert("uk-1 : uk-prod - ALBUnhealthyHostCritical")) == "alb_health"
          and follow_up.minutes_for("alb_health") == [3.0, 6.0])
    check("other families keep the 5/10 cadence",
          follow_up.minutes_for("thanos_usage") == [5.0, 10.0])
    check("the load balancer is read from the summary's ARN tail",
          follow_up._ALB_IN_TEXT.findall(
              "Application Load Balancer app/uk-prod-fastcore-app-http/992c86a8ba9f5fb3 has")
          == ["app/uk-prod-fastcore-app-http/992c86a8ba9f5fb3"])

    check("a CloudWatch-alarm alert gets the repeated cloudwatch_usage re-check",
          follow_up.kind_for(alert("Rekognition-ThrottledCount-High-wpsc01"))
          == "cloudwatch_usage")
    check("an RDS alert gets the rds_usage re-check",
          follow_up.kind_for(alert("[FIRING:1] us-1 : prod - RDS_CPUUtilizationAvgCriticalCore"))
          == "rds_usage")
    check("a pod alert gets no re-check",
          follow_up.kind_for(alert("KubePodCrashLooping")) == "")

    # The resolver must never pick a database when the alert is ambiguous.
    logged: list = []
    original = follow_up.aws_client.list_db_instances
    follow_up.aws_client.list_db_instances = lambda: [
        {"DBInstanceIdentifier": "prod-core-rds"},
        {"DBInstanceIdentifier": "prod-media-rds"},
        {"DBInstanceIdentifier": "stage-core-rds2"},
    ]
    try:
        exact = follow_up._rds_instance_for(
            alert("RDS CPU high on stage-core-rds2"), logged.append)
        check("an instance named in the alert is used verbatim",
              exact == "stage-core-rds2", exact)
        role = follow_up._rds_instance_for(
            alert("[FIRING:1] us-1 : prod - RDS_CPUUtilizationAvgCriticalCore"), logged.append)
        check("prod + core resolves to the prod core database", role == "prod-core-rds", role)
        logged.clear()
        vague = follow_up._rds_instance_for(alert("RDS_CPUUtilizationAvgCriticalCore"),
                                           logged.append)
        check("an alert naming no database resolves to nothing, not a guess",
              vague == "", f"{vague!r} {logged}")
    finally:
        follow_up.aws_client.list_db_instances = original


def test_promql_scoping() -> None:
    """A cluster-wide rule expression must be scoped to the resource the alert
    named before the re-check quotes a number from it."""
    from oncall_agent import follow_up

    group("promql scoping for re-checks")

    expr = ('(vmware_host_cpu_usage{cluster_name=~"RealMatch-Cluster01|RealMatch-Cluster03"} '
            '/ vmware_host_cpu_max) * 100')
    scoped = follow_up._scoped_expr(expr, 'host_name="ny1esx9679.verimatch.com"')
    check("both halves of the expression get the host",
          scoped.count('host_name="ny1esx9679.verimatch.com"') == 2, scoped)
    check("the rule's own selector is preserved",
          'cluster_name=~"RealMatch-Cluster01|RealMatch-Cluster03"' in scoped, scoped)
    check("function names never gain a selector",
          follow_up._scoped_expr("sum(rate(x_metric_total[5m])) > 1", 'a="b"')
          == 'sum(rate(x_metric_total{a="b"}[5m])) > 1',
          follow_up._scoped_expr("sum(rate(x_metric_total[5m])) > 1", 'a="b"'))
    check("no scope leaves the expression untouched",
          follow_up._scoped_expr(expr, "") == expr)
    check("panel variable maps to the series label",
          follow_up._scope_selector({"variables": '{"var-esxhost": "ny1esx9679.verimatch.com"}'})
          == 'host_name="ny1esx9679.verimatch.com"')
    check("an unknown variable yields no scope, not a guessed label",
          follow_up._scope_selector({"variables": '{"var-mystery": "x"}'}) == "")


def test_mention_escaping() -> None:
    """An HTML-escaped Slack mention posts as literal text and silently drops the
    ping. Seen for real on a 2026-08-27 Kubernetes run, where it also threw the
    whole investigation away on the mention-must-appear-in-a-post guardrail."""
    from oncall_agent.investigate import _unescape_slack_delimiters, _validate
    from oncall_agent.types import ParsedAlert

    group("slack mention/link escaping")

    check("escaped mention is repaired",
          _unescape_slack_delimiters("&lt;@devops-oncall&gt; FYI") == "<@devops-oncall> FYI")
    check("escaped link is repaired",
          _unescape_slack_delimiters("see &lt;https://x.test/a|the run&gt;")
          == "see <https://x.test/a|the run>")
    check("ordinary text with a stray entity is left alone",
          _unescape_slack_delimiters("free space &lt; 1GB") == "free space &lt; 1GB")
    check("real mention is untouched",
          _unescape_slack_delimiters("<@U123> FYI") == "<@U123> FYI")

    alert = ParsedAlert(source="victorops", kind="incident", raw_text="x")
    payload = {"should_post": True, "reasoning": "", "prior_incidents": [],
               "root_cause_narrative": "", "owning_team_mention": "&lt;@devops-oncall&gt;",
               "posts": [{"text": "*Pod:* `x` down", "evidence_keys": []},
                         {"text": "&lt;@devops-oncall&gt; FYI", "evidence_keys": []}]}
    decision, _ = _validate(payload, alert, "", evidence_keys=[])
    check("an escaped mention no longer aborts the run",
          decision.posts[-1].text == "<@devops-oncall> FYI", decision.posts[-1].text)

    payload["posts"] = [{"text": "*Pod:* `x` down", "evidence_keys": []}]
    raised = False
    try:
        _validate(payload, alert, "", evidence_keys=[])
    except RuntimeError:
        raised = True
    check("a mention in NO post is still a hard error", raised)


def test_kubectl_guards() -> None:
    """The read-only kubectl surface: names can never become flags, verbs are
    fixed, prod is opt-in, and secrets never leave in log output."""
    from oncall_agent.Agents.K8S_Agent import kubectl_client as kube

    group("kubectl guards (no cluster needed)")

    for bad in ("--as=cluster-admin", "-n kube-system", "pod;rm -rf /", "Pod_UPPER",
                "", "x" * 300, "../etc/passwd"):
        raised = False
        try:
            kube._validate("pod", bad)
        except kube.KubectlError:
            raised = True
        check(f"rejects pod name {bad[:24]!r}", raised)
    check("accepts a real pod name",
          kube._validate("pod", "discovery-app-stg198-5cf8bdc484-4l4mz").endswith("4l4mz"))

    check("only workload kinds are readable",
          all(_refuses_kind(kube, k) for k in ("secret", "configmap", "node", "clusterrole")),
          "a secret must never be readable through this surface")

    check("prod is not in the default environment allow-list",
          kube.ALLOWED_ENVIRONMENTS == ["stage"] or "KUBE_ENVIRONMENTS" in os.environ,
          f"got {kube.ALLOWED_ENVIRONMENTS}")

    scrubbed = kube._scrub(
        "Authorization: Bearer abcdefghij1234567890\n"
        "password=hunter2000\n"
        "AKIAIOSFODNN7EXAMPLE\n"
        "token: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w\n"
        "plain log line about a pod")
    for secret in ("abcdefghij1234567890", "hunter2000", "AKIAIOSFODNN7EXAMPLE"):
        check(f"log scrubber removes {secret[:12]!r}", secret not in scrubbed, scrubbed)
    check("log scrubber keeps ordinary log text",
          "plain log line about a pod" in scrubbed)


def _refuses_kind(kube, kind: str) -> bool:
    """workload() must refuse anything that is not a workload — a Secret read
    through the same code path would put credentials in a Slack thread."""
    from oncall_agent.Agents.K8S_Agent.kubectl_client import Cluster
    fake = Cluster(name="x", environment="stage", kubeconfig=Path("/nonexistent"))
    try:
        kube.workload(fake, kind, "default", "anything")
    except kube.KubectlError as e:
        return "not a workload kind" in str(e)
    except Exception:                                   # noqa: BLE001
        return False
    return False


# ---------------------------------------------------------------------- mcp

def test_mcp_server() -> None:
    group("slack mcp server (protocol only, no Slack calls)")
    requests = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {}}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "list_channels", "arguments": {}}}),
    ]) + "\n"
    py_dir = str(_ROOT / "py")
    proc = subprocess.run(
        [sys.executable, "-m", "oncall_agent.slack_mcp_server"],
        input=requests, capture_output=True, text=True, timeout=60,
        cwd=py_dir, env={"PATH": "/usr/bin:/bin", "PYTHONPATH": py_dir,
                         "HOME": str(Path.home())},
    )
    responses = {}
    for line in proc.stdout.splitlines():
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        responses[payload.get("id")] = payload

    check("initialize answered", responses.get(1, {}).get("result", {}).get("protocolVersion"))
    check("notification produced no response", None not in responses,
          "a reply to a notification breaks the protocol")
    tools = [t["name"] for t in responses.get(2, {}).get("result", {}).get("tools", [])]
    check("the five read-only tools are listed",
          sorted(tools) == ["list_channels", "read_channel", "read_thread",
                            "search_messages", "search_recent_changes"],
          f"got {tools}")
    from oncall_agent import slack_mcp_server as _slack_server

    unconfigured = _slack_server.CHANGE_CHANNELS
    _slack_server.CHANGE_CHANNELS = []
    try:
        answer = _slack_server.tool_search_recent_changes("jenkins.veritone.com")
    finally:
        _slack_server.CHANGE_CHANNELS = unconfigured
    check("an unconfigured change search reports itself unavailable",
          "not configured" in answer and "unavailable" in answer
          and "Do not conclude anything about planned work" in answer,
          answer[:160])

    check("no write tool is exposed",
          not any(w in t for t in tools for w in ("post", "send", "write", "update", "delete")),
          "reads and writes must stay on separate credentials")
    check("tools/call returns content",
          "C909ZH4ET" in json.dumps(responses.get(3, {}).get("result", {})))

    group("k8s / aws / edge ui / runscope / github / jira mcp servers (protocol only)")
    listing_only = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {}}}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
    ]) + "\n"
    for module_name, expected_min_tools in (
        ("oncall_agent.Agents.K8S_Agent.server", 5),
        ("oncall_agent.Agents.AWS_Agent.server", 11),
        ("oncall_agent.Agents.Windows_Agent.server", 2),
        ("oncall_agent.Agents.Edgeui_Agent.server", 11),
        ("oncall_agent.Agents.Runscope_Agent.server", 1),
        ("oncall_agent.Agents.Github_Agent.server", 5),
        ("oncall_agent.Agents.Jira_Agent.server", 3),
    ):
        proc = subprocess.run(
            [sys.executable, "-m", module_name],
            input=listing_only, capture_output=True, text=True, timeout=60,
            cwd=py_dir, env={"PATH": "/usr/bin:/bin", "PYTHONPATH": py_dir,
                             "HOME": str(Path.home())},
        )
        tools = []
        for line in proc.stdout.splitlines():
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if payload.get("id") == 2:
                tools = [t["name"] for t in payload["result"]["tools"]]
        check(f"{module_name} lists at least {expected_min_tools} tool(s)",
              len(tools) >= expected_min_tools, f"got {tools}")
        check(f"{module_name} exposes no write/delete/restart tool",
              not any(w in t for t in tools for w in
                     ("delete", "restart", "scale", "resize", "post", "send")),
              f"got {tools}")


def main() -> int:
    print("oncall-agent offline tests (no credentials, no network)")
    for suite in (test_parser, test_guardrails, test_routing, test_specialist_routing,
                 test_formatting, test_api_health_pairing, test_follow_up,
                 test_promql_scoping, test_mention_escaping, test_kubectl_guards,
                 test_aws_guards,
                 test_mcp_server):
        try:
            suite()
        except Exception as e:                      # noqa: BLE001 — report, keep going
            _failed.append(f"{suite.__name__} raised {type(e).__name__}: {e}")
            print(f"  ERROR {suite.__name__}: {type(e).__name__}: {e}")

    print(f"\n{_passed} passed, {len(_failed)} failed")
    for failure in _failed:
        print(f"  - {failure}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
