#!/usr/bin/env python3
"""
Preflight: does this machine actually have everything the agent needs?

  python py/scripts/check_setup.py

Every dependency is checked independently and reported PASS / WARN / FAIL,
because most of them degrade rather than break: no Grafana means no graph
panels but a full triage; no channels:history means no past-thread grounding
but the offline case library still applies; no Edge credentials means only
the generic route works. Only Slack read access is genuinely required.

Nothing here writes to Slack.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import bootstrap  # noqa: E402  — must precede third-party imports

bootstrap()

from dotenv import load_dotenv

from oncall_agent import llm
from oncall_agent.Agents import registry as specialists
from oncall_agent.Agents.Edgeui_Agent.edge_environments import load_edge_environments
from oncall_agent.Agents.Grafana_Agent import grafana
from oncall_agent.config import describe, load_config
from oncall_agent.evidence_panels import load_panel_map

_STATUS = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}
_failures = 0
_warnings = 0


def report(status: str, name: str, detail: str) -> None:
    global _failures, _warnings
    if status == "fail":
        _failures += 1
    elif status == "warn":
        _warnings += 1
    print(f"  {_STATUS[status]:<5} {name:<22} {detail}")


def check_slack(config) -> None:
    if not config.slack_bot_token:
        report("fail", "slack token", "SLACK_BOT_TOKEN unset — the agent cannot read alerts")
        return
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    client = WebClient(token=config.slack_bot_token)
    try:
        auth = client.auth_test()
        report("pass", "slack auth", f"{auth.get('user')} @ {auth.get('team')} ({auth.get('url')})")
    except SlackApiError as e:
        report("fail", "slack auth", str(e.response.get("error", e)))
        return

    # A token can only ever see its OWN workspace, and that is worth checking
    # first: otherwise the symptom is `missing_scope`/`channel_not_found` on
    # every read, which sends you off adding scopes that cannot possibly help.
    # ASK Slack rather than pattern-matching the channel id — an earlier
    # version assumed any "C..." id was Veritone and cried wolf on a correctly
    # configured test workspace.
    workspace = (auth.get("url") or "").rstrip("/").replace("https://", "")
    unreachable = []
    for name, channel in (("ALERTS_CHANNEL", config.alerts_channel),
                          ("SLACK_CHANNEL", config.comms_channel)):
        try:
            client.conversations_info(channel=channel)
        except SlackApiError as e:
            if e.response.get("error") == "channel_not_found":
                unreachable.append(f"{name}={channel}")
    if unreachable:
        report("fail", "workspace",
               f"the token belongs to {workspace}, which cannot see "
               f"{', '.join(unreachable)}. A Slack token only ever reaches its own "
               f"workspace — you need a credential issued where those channels live.")
    else:
        report("pass", "workspace", f"token and channels are both in {workspace or '(unknown)'}")

    for label, channel in (("#alerts-devops read", config.alerts_channel),
                           ("#comms-noc history", config.history_channel)):
        try:
            res = client.conversations_history(channel=channel, limit=1)
            n = len(res.get("messages", []))
            report("pass", label, f"{channel} readable ({n} recent message visible)")
        except SlackApiError as e:
            error = e.response.get("error", str(e))
            hint = {
                "missing_scope": "bot token lacks channels:history / groups:history",
                "not_in_channel": "invite the app to the channel",
                "channel_not_found": "wrong id, or a private channel the app cannot see",
            }.get(error, "")
            level = "fail" if "alerts" in label else "warn"
            report(level, label, f"{channel}: {error}" + (f" — {hint}" if hint else ""))

    if config.socket_mode:
        report("pass", "transport", "SLACK_APP_TOKEN set — socket mode, buttons enabled")
    else:
        report("warn", "transport", "no SLACK_APP_TOKEN — polling mode, no interactive buttons")

    if config.slack_user_token:
        user_client = WebClient(token=config.slack_user_token)
        try:
            auth = user_client.auth_test()
            report("pass", "slack user token", f"reads as {auth.get('user')} — search + history available")
        except SlackApiError as e:
            report("warn", "slack user token", f"rejected: {e.response.get('error', e)}")
    else:
        report("warn", "slack user token",
               "SLACK_USER_TOKEN unset — the agent cannot search #comms-noc for itself; "
               "investigation falls back to keyword prefetch")


def check_investigation(config) -> None:
    """Which investigation path will actually run, and can it start."""
    from oncall_agent.investigate import DEFAULT_MCP_CONFIG, SLACK_TOOLS

    mcp_exists = DEFAULT_MCP_CONFIG.exists()
    # Grafana alone justifies the tool path — omitting it here reported
    # "prefetch" on a setup that would actually run tools.
    mode = config.investigation_mode(mcp_exists, grafana_configured=grafana.is_configured())
    if not mcp_exists:
        report("warn", "mcp config", f"{DEFAULT_MCP_CONFIG} missing — tool investigation unavailable")
    if mode == "tools":
        report("pass", "investigation", f"tools — Claude reads Slack itself via {len(SLACK_TOOLS)} read-only tools")
    else:
        report("warn", "investigation",
               "prefetch — we keyword-fetch #comms-noc threads and hand them over "
               "(needs .mcp.json plus Grafana or SLACK_USER_TOKEN for the tool path)")

    # Prove each MCP server starts and speaks the protocol. Each runs as a
    # subprocess under the claude CLI, so an import error there would only
    # ever show up mid-incident as "no tools" for whichever specialist needed it.
    if not mcp_exists:
        return
    for module_name, label in (
        ("oncall_agent.slack_mcp_server", "slack mcp server"),
        ("oncall_agent.Agents.Grafana_Agent.server", "grafana mcp server"),
        ("oncall_agent.Agents.Edgeui_Agent.server", "edge ui mcp server"),
        ("oncall_agent.Agents.Runscope_Agent.server", "runscope mcp server"),
        ("oncall_agent.Agents.Github_Agent.server", "github mcp server"),
        ("oncall_agent.Agents.Jira_Agent.server", "jira mcp server"),
    ):
        _check_mcp_server(module_name, label)


def _check_mcp_server(module_name: str, label: str) -> None:
    import json as _json
    import subprocess
    handshake = _json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "2024-11-05", "capabilities": {}}})
    listing = _json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    try:
        proc = subprocess.run(
            [sys.executable, "-m", module_name],
            input=f"{handshake}\n{listing}\n", capture_output=True, text=True, timeout=30,
            cwd=str(Path(__file__).resolve().parent.parent),
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)},
        )
        names = []
        for line in proc.stdout.splitlines():
            payload = _json.loads(line)
            if payload.get("id") == 2:
                names = [t["name"] for t in payload["result"]["tools"]]
        if names:
            report("pass", label, f"responds with {len(names)} tools: {', '.join(names)}")
        else:
            report("fail", label, f"no tool list returned: {proc.stderr[:120]}")
    except Exception as e:                          # noqa: BLE001
        report("fail", label, f"{type(e).__name__}: {str(e)[:120]}")


def check_llm() -> None:
    try:
        if llm.PROVIDER == "claude_cli":
            report("pass", "llm", f"claude_cli via {llm.claude_binary()} (model {llm.CLAUDE_CLI_MODEL})")
        elif llm.PROVIDER == "anthropic":
            has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
            report("pass" if has_key else "fail", "llm",
                   f"anthropic {llm.ANTHROPIC_MODEL}" + ("" if has_key else " — ANTHROPIC_API_KEY unset"))
        else:
            report("fail", "llm", f"unknown LLM_PROVIDER={llm.PROVIDER!r}")
    except RuntimeError as e:
        report("fail", "llm", str(e))


def check_grafana() -> None:
    if not grafana.is_configured():
        report("warn", "grafana", "GRAFANA_URL/GRAFANA_API_TOKEN unset — no graph panels attached")
        return
    try:
        health = grafana.health()
        report("pass", "grafana", f"{os.environ['GRAFANA_URL']} v{health.get('version')}")
    except (RuntimeError, SystemExit) as e:
        report("warn", "grafana", str(e).splitlines()[0])
        return
    # Asked of /api/plugins, not /render/: a Grafana with no renderer answers
    # every /render/ URL with a 200 and a placeholder PNG that merely says so.
    if grafana.renderer_available():
        report("pass", "grafana renderer", "image-renderer plugin installed — /render API in use")
    else:
        # Not a failure: the browser fallback is the working path here. Kept a
        # WARN only because installing the plugin server-side would be faster.
        report("warn", "grafana renderer",
               "no image-renderer plugin — using headless-browser capture instead "
               "(works; ~5s/panel). Install the plugin server-side for the faster "
               "/render path, or GRAFANA_CAPTURE=off to skip graphs.")


def check_panel_map() -> None:
    panel_map = load_panel_map()
    mapped = len(panel_map._index)
    if mapped:
        report("pass", "panel map", f"{mapped} alertname variant(s) mapped to panels")
    else:
        report("warn", "panel map", "config/panel_map.json is empty — no Grafana panels will attach "
                                    "(fill it with `python -m oncall_agent.Agents.Grafana_Agent.grafana --list-panels <uid>`)")


def check_cases() -> None:
    # Each specialist owns its own data/cases.json now (2026-08-26) — check
    # every one individually so a specialist quietly losing its case file
    # doesn't hide behind the others still having theirs.
    total = 0
    for name, cfg in specialists.SPECIALISTS.items():
        cases = cfg["case_library"]
        total += len(cases.cases)
        if cases.cases:
            report("pass", f"{name} cases", f"{len(cases.cases)} case(s), "
                                            f"{len(cases.known_alertnames)} alertname variant(s)")
        else:
            report("warn", f"{name} cases", f"Agents/.../data/cases.json missing or empty for "
                                            f"{name!r} — that specialist loses its historical grounding")
    if not total:
        report("warn", "case library", "no specialist has any cases — triage loses its historical grounding")


def check_edge() -> None:
    envs = load_edge_environments()
    if envs:
        report("pass", "edge environments", f"{len(envs)} configured: {', '.join(sorted(envs))}")
    else:
        report("warn", "edge environments", "none configured — engine-failure alerts cannot be investigated")
    if os.environ.get("EDGE_USERNAME") and os.environ.get("EDGE_PASSWORD"):
        report("pass", "edge ui login", "EDGE_USERNAME/EDGE_PASSWORD set (needed for screenshots)")
    else:
        report("warn", "edge ui login", "EDGE_USERNAME/EDGE_PASSWORD unset — no Edge UI screenshots")
    # Import is not enough: chromium also needs its OS libraries, and the
    # failure mode without them (TargetClosedError on launch) is opaque enough
    # to be worth catching here instead of mid-incident.
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        report("warn", "playwright", "not installed — no Edge UI or Grafana screenshots")
        return
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            version = browser.version
            browser.close()
        report("pass", "playwright", f"chromium {version} launches")
    except Exception as e:                          # noqa: BLE001
        first = str(e).splitlines()[0]
        detail = "missing OS libraries — run `playwright install-deps` (needs sudo)" \
            if "shared libraries" in str(e) or "TargetClosedError" in type(e).__name__ \
            else first
        report("warn", "playwright", f"chromium cannot launch: {detail}")


def check_github() -> None:
    import shutil
    import subprocess

    if not shutil.which("gh"):
        report("warn", "github cli", "`gh` not installed — the Edge UI specialist's GitHub tools "
                                     "(search_code/get_file/get_pr/get_commit/list_workflow_runs) "
                                     "will fail on every call")
        return
    try:
        who = subprocess.run(["gh", "api", "user", "--jq", ".login"],
                             capture_output=True, text=True, timeout=15)
    except Exception as e:                              # noqa: BLE001
        report("warn", "github cli", f"`gh api user` failed to run: {type(e).__name__}: {e}")
        return
    if who.returncode != 0:
        report("warn", "github cli", f"not authenticated — run `gh auth login`. "
                                     f"({(who.stderr or who.stdout).strip().splitlines()[0]})")
        return
    report("pass", "github cli", f"gh authenticated as {who.stdout.strip()}")
    # Org membership isn't the same as SSO authorization for that specific
    # token — an org with SAML SSO enforced (veritone is one) blocks API
    # access until a human clicks through https://github.com/orgs/<org>/sso,
    # separately from being a member. Only warn, never fail: repos outside
    # an SSO-enforcing org still work fine with this same token.
    report("warn", "github sso", "org membership doesn't guarantee API access — an SSO-enforcing "
                                 "org (e.g. veritone) blocks this token until you authorize it at "
                                 "https://github.com/orgs/<org>/sso. Not checked here per-org; if a "
                                 "GitHub tool call fails with a SAML enforcement error, that's the fix.")


def check_jira() -> None:
    site = os.environ.get("JIRA_SITE")
    email = os.environ.get("JIRA_EMAIL")
    token = os.environ.get("JIRA_API_TOKEN")
    if not (site and email and token):
        report("warn", "jira", "JIRA_SITE/JIRA_EMAIL/JIRA_API_TOKEN unset — no specialist can check "
                               "for an existing Jira ticket related to an alert")
        return
    import requests
    try:
        res = requests.get(f"{site.rstrip('/')}/rest/api/3/myself",
                           auth=requests.auth.HTTPBasicAuth(email, token),
                           headers={"Accept": "application/json"}, timeout=15)
    except Exception as e:                              # noqa: BLE001
        report("warn", "jira", f"request failed to run: {type(e).__name__}: {e}")
        return
    if res.ok:
        who = res.json().get("displayName", email)
        report("pass", "jira auth", f"{site} reads as {who}")
    else:
        report("warn", "jira auth", f"{res.status_code} {res.reason} — check JIRA_EMAIL/JIRA_API_TOKEN")


def main() -> int:
    load_dotenv()
    config = load_config()
    print(f"oncall-agent preflight\n  {describe(config)}\n")
    if config.profile == "production":
        print("  *** PROFILE=production — these are the REAL NOC channels ***\n")
    check_slack(config)
    check_investigation(config)
    check_llm()
    check_grafana()
    check_panel_map()
    check_cases()
    check_edge()
    check_github()
    check_jira()
    print(f"\n{_failures} failure(s), {_warnings} warning(s).")
    if config.post_mode == "dry_run":
        print("POST_MODE=dry_run — nothing will be posted to Slack until you set POST_MODE=live.")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
