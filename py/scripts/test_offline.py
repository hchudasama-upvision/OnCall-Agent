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


# --------------------------------------------------------------- formatting

def test_formatting() -> None:
    from oncall_agent.alert_parser import parse_alert_message, to_victorops_incident
    from oncall_agent.slack_blocks import SECTION_LIMIT, TEXT_LIMIT, fallback_text, sections
    from oncall_agent.case_library import load_case_library
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

    cases = load_case_library()
    check("case library loaded", len(cases.cases) > 0, f"{len(cases.cases)} cases")
    check("case fingerprinting works",
          cases.fingerprint("[FIRING:2] aiw-prd5001 - KubePersistentVolumeFillingUp (x)")
          == "KubePersistentVolumeFillingUp")


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
    check("four read-only tools listed",
          sorted(tools) == ["list_channels", "read_channel", "read_thread", "search_messages"],
          f"got {tools}")
    check("no write tool is exposed",
          not any(w in t for t in tools for w in ("post", "send", "write", "update", "delete")),
          "reads and writes must stay on separate credentials")
    check("tools/call returns content",
          "C909ZH4ET" in json.dumps(responses.get(3, {}).get("result", {})))


def main() -> int:
    print("oncall-agent offline tests (no credentials, no network)")
    for suite in (test_parser, test_guardrails, test_routing, test_formatting, test_mcp_server):
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
