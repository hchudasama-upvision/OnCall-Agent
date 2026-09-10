# OnCall-Agent — NOC on-call alert automation

This repo is the NOC on-call automation agent.

It watches **`#alerts-devops`**, investigates each paged incident the way an
on-call engineer does — grounded in what **`#comms-noc`** actually did for
that alert type before — and posts the evidence (Grafana graphs, Edge UI /
Controller screenshots, task and job logs) into the incident's `#comms-noc`
thread.

```
#alerts-devops ──► parse ──► dedupe / suppress ──► route
   (VictorOps         │                              │
    incident cards)   │                    ┌─────────┴──────────┐
                      │                    ▼                    ▼
                      │        engine-failure rate        everything else
                      │        ────────────────────       ──────────────
                      │        Edge UI task stats         case library
                      │        Edge UI screenshots        + past #comms-noc
                      │        task + job log .zips         threads
                      │        (NOT Grafana — this        + #alerts-devops
                      │         alert is an Edge UI         correlation
                      │         investigation)            + mapped Grafana panels
                      │                    ▼                    ▼
                      │            decide_resolution      7-section triage
                      │            (structured, schema-    (What fired /
                      │             validated, guardrailed) Seen before / …)
                      │                    │                    │
                      └────────────────────┴────────┬───────────┘
                                                     ▼
                                        #comms-noc: *Alert:* + threaded
                                        evidence + escalation @mention
```

**Code decides whether to act; the model decides what to gather.** Triggering,
suppression, dedupe, routing and every guardrail are deterministic. The
investigation itself is live: the model searches Grafana, reads what a panel
actually queries, runs PromQL to turn what the alert gives it into what the
panel needs, renders, and retries if the render comes back empty — the loop a
human runs. It still cannot attach a screenshot that was not rendered, cite a
link it did not read, or execute anything.

Worked example — alert `High memory utilization (>95% for 5m) - SVC182`, no
configuration for it anywhere:

```
16 tool calls, $0.60
  search_dashboards("memory")          -> 2. Windows Server Details
  describe_dashboard(Sad3g6Y2d)        -> panel 21 filters on $instance, not $hostname
  prometheus_query(windows_os_hostname{hostname="SVC182"})
                                       -> instance=10.60.4.182:9182
  render_panel(21, var-instance=...)   -> evidence_key svc182_memory_gauge
```

It found the same dashboard, panel and variables that had previously been
hand-encoded — by looking, not by being told.

## How the investigation reads Slack

The interesting part of an alert is not the alert — it is what `#comms-noc`
did the last five times it fired. There are two ways the agent gets that, and
which one runs is `INVESTIGATION` (default `auto`):

**`tools` — Claude reads the channel itself.** It searches `#comms-noc` for
the alert type, opens the threads, and reads what was actually *done*, the
way a person picking up an unfamiliar page would. This finds things a keyword
prefetch cannot: for a `aiw-prod1001` engine-failure page it surfaces that the
cause is a known core partitioning defect (VE-23618), that ~1,872 stuck tasks
will keep it re-firing, and that the one check worth a human's time is whether
the failed tasks were created 14–16 Aug (known) or recently (new).

**`prefetch` — we hand it threads.** Keyword-matched `#comms-noc` history is
fetched and pasted into the prompt. Cheaper, no extra token, finds only what
the keyword guessed.

`auto` picks `tools` when `SLACK_USER_TOKEN` is set, else `prefetch`.

### Where the tools come from

Headless `claude -p` **cannot** use the claude.ai Slack connector — it answers
`NO_SLACK_TOOLS` (verified 2026-08-24; the connector is bound to the
interactive session). But the CLI accepts `--mcp-config`, so the tools come
from `oncall_agent/slack_mcp_server.py`, a small read-only stdio MCP server in
this repo: `read_channel`, `read_thread`, `search_messages`, `list_channels`.

Reads and writes deliberately use different credentials:

| | token | why |
|---|---|---|
| **read** `#alerts-devops`, `#comms-noc` | `SLACK_USER_TOKEN` (xoxp) | needs `channels:history` + `search:read`, which the bot app does not have |
| **write** the incident thread | `SLACK_BOT_TOKEN` (xoxb) | already has `chat:write` + `files:write` |

The MCP server exposes **no write tool at all**, and `investigate.py`
allow-lists only those four names. A prompt that can read a channel can never
post to it — which matters, because alert payloads are untrusted input.

### The no-fabrication guarantee still holds

Letting the model fetch its own sources would normally destroy any promise
that the links in a posted thread are real. It does not here: the run uses
`--output-format stream-json`, so **every `tool_result` the model received is
captured**, and the output is validated against that corpus before anything
reaches Slack. Verified rejections:

```
ACCEPTED  URL that was actually read
REJECTED  fabricated jira url
REJECTED  fabricated slack permalink
REJECTED  path extension off a real host   (…/browse/VE-23618 seen → …/browse/VE-99999 refused)
REJECTED  invented prior-thread timestamp
REJECTED  evidence key that was never captured
REJECTED  escalation mention decided but absent from every reply
```

Matching is exact, not prefix — a real host must not authorize an invented
path under it.

### Posting without the read scope: `post_decision.py`

Because reading and posting are separate credentials, they are separate
steps — so whoever *can* read may investigate, and the bot posts the result:

```bash
python py/scripts/post_decision.py decision.json                     # dry run
python py/scripts/post_decision.py decision.json --live
python py/scripts/post_decision.py decision.json --live --thread-ts 1785840930.320989
python py/scripts/post_decision.py --print-schema
```

That works **today, with no new Slack scopes**: Claude investigates in an
interactive session (where the connector does work), writes the decision JSON,
and this posts it with the bot token. The unattended path produces the exact
same JSON through the MCP server — one format, two producers.

`--thread-ts` appends to an existing thread. Recurrences of an alert are
appended to the original thread today, not posted fresh; on `aiw-prod1001`
every engine-failure page since 16 Aug hangs off one thread.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r py/requirements.txt
playwright install chromium && playwright install-deps   # install-deps needs sudo

pip install -e .            # puts oncall_agent on the path (see note below)

cp .env.example .env        # then fill it in
python py/scripts/test_offline.py     # 57 checks, no credentials needed
python py/scripts/check_setup.py      # what this machine can actually reach
```

> The package lives under `py/`, not at the repo root. `pip install -e .` is
> what makes `python -m oncall_agent.Agents.Grafana_Agent.grafana` work from any directory. Without
> it you must prefix those commands with `PYTHONPATH=py`. The
> `py/scripts/*.py` entry points work either way — they put `py/` on the path
> themselves.

## Testing

Three levels, cheapest first.

**1. Offline — no credentials, no network, ~2s.** Run this after any change.

```bash
python py/scripts/test_offline.py        # 57 checks
```

Covers the parser against real captured alert cards, the anti-fabrication
guardrails, trigger/suppress/dedupe routing, the `#comms-noc` header format,
Slack's two size caps, and the MCP server's protocol handshake. Every parser
assertion in it is a bug that shipped once.

**2. Connectivity — reads only, posts nothing.**

```bash
python py/scripts/check_setup.py                    # every dependency, PASS/WARN/FAIL
python -m oncall_agent.slack_mcp_server --selftest  # names the exact Slack scopes you have
python -m oncall_agent.Agents.Grafana_Agent.grafana --check              # reachability, token, renderer
python -m oncall_agent.Agents.Grafana_Agent.grafana --list-dashboards engine
python -m oncall_agent.Agents.Grafana_Agent.grafana --render <uid>:<id> -o /tmp/p.png
```

(The `python -m` commands need `pip install -e .` or a `PYTHONPATH=py` prefix.)

**3. End to end — investigates for real, still posts nothing.**

```bash
python py/scripts/run_listener.py --once                             # one pass over new alerts
python py/scripts/run_listener.py --message-url <slack permalink>    # replay one specific alert
python py/scripts/post_decision.py decision.json                     # dry-run a drafted thread
```

Only `--live` (or `POST_MODE=live`) ever writes to Slack. Point `SLACK_CHANNEL`
at a test channel before the first live run.

Almost everything degrades rather than breaks — no Grafana means no graphs
but a full investigation; no `channels:history` means no past-thread
grounding but the offline case library still applies. Only Slack read access
is genuinely required.

## Running it

```bash
# Follow #alerts-devops. Dry run by default: investigates for real, prints
# the thread instead of posting.
python py/scripts/run_listener.py

# One pass over new messages, then exit (good for cron).
python py/scripts/run_listener.py --once

# Re-investigate one specific historical alert and compare the agent's
# thread against what the engineer actually wrote that night.
python py/scripts/run_listener.py --message-url https://veritone.slack.com/archives/C909ZH4ET/p175...

# Actually post to Slack.
python py/scripts/run_listener.py --live        # or POST_MODE=live in .env
```

Manual entry point for one engine-failure incident (unchanged CLI):

```bash
python py/scripts/run_live_test.py \
  "Incident #119875: [FIRING:1] aiw-prd5001 : Engine failure rate above 15%" \
  "SI2 Playback segment creator" 15 [slackPermalink]
```

Both paths run the same pipeline code.

### Running continuously

`run_listener.py` with no flags is the daemon: it polls `#alerts-devops` every
`POLL_SECONDS`, and for each new VictorOps incident it dedupes, routes,
investigates and (in live mode) posts. It keeps a cursor in `.state/` so a
restart does not re-investigate what it already saw.

```bash
# foreground, dry run — watch what it would do
python py/scripts/run_listener.py

# background, posting for real
nohup python py/scripts/run_listener.py --live > oncall-agent.log 2>&1 &

# or on a schedule instead of a daemon — --once is cursor-aware, so cron
# every 2 minutes behaves the same as polling
*/2 * * * * cd /path/to/OnCall-Agent && .venv/bin/python py/scripts/run_listener.py --once --live >> cron.log 2>&1
```

Set `SLACK_APP_TOKEN` and it switches from polling to Socket Mode — real-time
instead of up-to-`POLL_SECONDS` late, and the Approve/Deny buttons work.

### Test vs production

`PROFILE` decides which channels the agent touches:

```bash
# .env — test workspace (the default)
PROFILE=test
ALERTS_CHANNEL=<test workspace alert channel>
SLACK_CHANNEL=<test workspace comms channel>

# .env — production
PROFILE=production          # defaults to Veritone #alerts-devops + #comms-noc
```

`PROFILE=test` has **no channel defaults** and refuses to start without
explicit ones. That is deliberate: the defaults used to be the real Veritone
channels, so an `.env` that merely forgot `ALERTS_CHANNEL` aimed the agent at
the live NOC channel — and `--live` would have posted there. Reaching
production is now a stated decision, and `check_setup.py` prints a banner when
the profile is production.

Keep two files and switch between them:

```bash
cp .env .env.test && cp .env .env.production   # edit each
ln -sf .env.test .env                          # or .env.production
```

`HISTORY_CHANNEL` is separate from where it posts, so a test run can still
ground itself in the real `#comms-noc` if the token can read it.

### Whose credentials is it using?

Worth knowing before this posts anywhere, because the `.env` in a fork is
usually inherited rather than yours:

| | identity | consequence |
|---|---|---|
| Slack | `nocautomationbot @ upvision-in.slack.com` | posts appear as that bot, in that workspace |
| Grafana | service account `sa-1-user_termination` | panel renders are attributed to it |
| Edge UI | `EDGE_USERNAME` — the Tasks screenshot shows the logged-in name | **every Edge screenshot the agent posts shows that person's session** |

The Edge one matters most: the agent attaches screenshots taken as a real
person, so evidence in an incident thread is visually attributed to someone
who is not operating the agent. DESIGN.md §5 already calls this out as Phase 0
hygiene — "dedicated service principals per action, replacing the personal
SSO-assumed roles currently baked into automation". Before production the
agent wants its own Slack app, its own Grafana service account, and its own
Edge bot account.

`config/slack_app_manifest.yaml` is the app definition, with the scopes the
listener needs. If reads fail with `missing_scope` while the manifest looks
right, the token predates the scopes — Slack fixes them at install time, so
**Install App -> Reinstall to Workspace** and take the new token.

**What it needs before any of that works.** A Slack token can only see its own
workspace, so the agent needs ONE credential issued by the workspace the
channels live in, able to both read the alert channel and post to the comms
channel:

| | needs | today |
|---|---|---|
| read `#alerts-devops` | `channels:history` | ✗ |
| post to `#comms-noc` | `chat:write`, `files:write` | ✗ for Veritone |

The current `SLACK_BOT_TOKEN` is `nocautomationbot @ upvision-in.slack.com`,
while `#alerts-devops` (C909ZH4ET) and `#comms-noc` (C01F810QM96) are on
`veritone.slack.com`. That is why every read fails, and adding scopes to that
app cannot fix it. `python py/scripts/check_setup.py` reports this as a FAIL on
the `workspace` line. Three ways forward:

1. **Veritone user token** (`xoxp-`, `channels:history` + `search:read` +
   `chat:write`). Works immediately and unlocks the tool-driven investigation.
   Posts appear as you, not as a bot — a policy call, not a technical one.
2. **A Slack app installed in the Veritone workspace** — the production
   answer, needs whoever administers Slack there.
3. **Run the loop in UpVision first** (self-serve, no approvals). Add
   `channels:history` to the existing app, point `ALERTS_CHANNEL` /
   `COMMS_CHANNEL` at channels in that workspace, and use
   `replay_alert.py --post` to fire alerts into it. Grafana and Edge evidence
   are unaffected — those are separate credentials and already work.

### Transport

| | polling (default) | Socket Mode |
|---|---|---|
| needs | `SLACK_BOT_TOKEN` | `+ SLACK_APP_TOKEN` (xapp-…, `connections:write`) |
| latency | `POLL_SECONDS` (60s) | real time |
| buttons | no | Approve / Deny (log-only) |

Set `SLACK_APP_TOKEN` and the listener switches automatically.

## Safety defaults

Both are opt-out in `.env`, and both exist because of DESIGN.md:

- **`POST_MODE=dry_run`** — §6 prescribes shadow mode before assist mode. The
  agent should not post into a live NOC incident channel the first time
  someone runs it.
- **`TRIGGER_ON=victorops`** — only VictorOps *incidents* start an
  investigation (the v1.1 scope refinement). Raw Alertmanager, PandoLogic and
  Jenkins posts are kept as correlation context; `#alerts-devops` carries
  dozens of those an hour.

On top of that: a fingerprint dedupe window (`DEDUPE_WINDOW_MINUTES`, default
60) collapses the same condition arriving via several paths, and
`config/suppression.json` holds known-chronic fingerprints that must never
open a thread (§1.5 item 1 — `aiw-stg198 KubePodCrashLooping` has fired every
five minutes since at least July).

## Configuration files

| File | What it holds |
|---|---|
| `.env` | every credential and every toggle — see `.env.example` |
| `config/panel_map.json` | alert type → Grafana dashboard + panel ids |
| `~/eks/<env>/<cluster>/` | kubeconfigs the Kubernetes specialist reads (generated by `~/eks/generate-kube-config.sh`; `KUBE_ENVIRONMENTS` gates which, stage-only by default) |
| `config/suppression.json` | chronic fingerprints to never investigate |
| `config/specialist_routing.json` | alert type → specialist agent (master router) |
| `py/oncall_agent/Agents/<Name>/data/cases.json` | each specialist's own sanitized cases from ~30 real incident threads (14 entries, split by domain — PVC filling up is deliberately in two of them, see its `_shared_with`) |

### Adding Grafana panels for an alert type

Panel ids are discovered, never guessed — a wrong id renders a real-looking
picture of the wrong graph.

```bash
python -m oncall_agent.Agents.Grafana_Agent.grafana --check              # reachability, token, renderer
python -m oncall_agent.Agents.Grafana_Agent.grafana --list-dashboards engine
python -m oncall_agent.Agents.Grafana_Agent.grafana --list-panels <dashboard_uid>
```

Then add an entry to `config/panel_map.json` (see `panel_map.example.json`
for both accepted shapes). `{env}` in a template-variable value is replaced
with the alert's `aiw-xxx` environment key.

> **Note on this Grafana:** `thanos-grafana.ops.veritone.com` has **no
> `grafana-image-renderer` plugin** (checked 2026-08-24 via `/api/plugins`).
> It does not fail loudly about it — every `/render/` URL answers HTTP 200
> with a valid PNG that reads *"No image renderer available/installed"*.
> `grafana.render_panel()` rejects that placeholder, and `GRAFANA_CAPTURE=auto`
> falls back to headless-browser capture of the `d-solo` panel page,
> authenticated with the same service-account token.
>
> That fallback is **verified working**: 3/3 panels captured in ~16s against a
> real templated dashboard. Installing the renderer plugin server-side would
> light up the faster `/render` path with no code change.
>
> `config/panel_map.json` ships **empty**, deliberately. Grafana is the right
> evidence for some alert types (NSQ backlog, API success rate, disk/VM
> alarms) and the wrong evidence for others — engine-failure rate is an Edge
> UI investigation and gets task counts, error type and logs instead. A
> dashboard rendering correctly is not evidence that it belongs on a given
> alert.

## Repository layout

```
py/oncall_agent/
  listener.py              watch #alerts-devops (socket mode or polling)
  alert_parser.py          the five #alerts-devops bot formats → ParsedAlert
  handler.py               dedupe, suppress, route (via Agents/MASTER_Agent), post
  investigate.py           shared tool-call machinery + no-fabrication validation
                           + the generalist fallback for alert types no
                           specialist claims
  agent_memory.py          per-specialist write-back memory: what an agent
                           actually found the last few times it saw this
                           exact alert type (.state/memory/<specialist>/)
  case_library.py          generic loader/matcher — each specialist's OWN
                           data/cases.json is the actual data (see below)
  slack_mcp_server.py      read-only Slack MCP server for headless claude —
                           shared by every specialist
  slack_history.py / slack_post.py / slack_blocks.py
  config.py                .env → AgentConfig
  evidence_panels.py       panel map → PNGs → thread
  triage.py / llm.py       legacy no-tools generalist path — superseded by
                           investigate.py's tool-using generalist, kept
                           unused as a rollback
  Agents/                  one folder per domain specialist (2026-08-26) —
                           each owns its own tools, system prompt, and case
                           library; only Slack + the deterministic router
                           are shared
    MASTER_Agent/          router.py — deterministic, gets the alert,
                           decides which specialist handles it
    registry.py            assembles Agents/<Name>/ into SPECIALISTS and
                           runs the `claude -p` subprocess call per domain
    Edgeui_Agent/           engine-failure specialist: server.py (Edge UI
                           MCP tools), prompt.py, data/cases.json, plus the
                           old deterministic pipeline (engine_failure_pipeline.py
                           /decide_resolution.py/live_edge_ui_client.py/
                           edge_api.py/edge_environments.py/
                           engine_task_stats.py/screenshot.py) kept as a
                           rollback, unused. The only specialist also given
                           Github_Agent's tools (below) — a real code
                           regression is the common case here.
    Grafana_Agent/          server.py (Grafana MCP tools: search/describe/
                           query/render), grafana.py, grafana_capture.py —
                           shared by K8S_Agent and grafana_metrics below
      grafana_metrics/      resource-alert specialist: prompt.py + its own
                           data/cases.json (disk/memory/ALB/API-rate cases)
    K8S_Agent/              Kubernetes specialist: server.py (read-only
                           kubectl: unhealthy pods, pod state/events, pod
                           logs, replica peers), kubectl_client.py,
                           prompt.py + its own data/cases.json. Keeps
                           Grafana_Agent's tools too — kubectl answers "what
                           is wrong with this pod", Prometheus answers "how
                           long has it been true"
    Runscope_Agent/         synthetic-test specialist: server.py, prompt.py,
                           runscope_client.py, data/cases.json
    Github_Agent/           read-only GitHub via the `gh` CLI's own existing
                           auth (see github_client.py's credential note):
                           search code, read a file, check a PR/commit,
                           list workflow runs. Not a routed specialist —
                           an extra toolset given to Edgeui_Agent only.
    Jira_Agent/             read-only Jira Cloud REST API (JIRA_EMAIL/
                           JIRA_API_TOKEN in .env, see jira_client.py):
                           search_issues/get_issue/list_comments, so a
                           specialist can find and cite an EXISTING ticket
                           that already tracks this exact issue instead of
                           only a case-library note. Not a routed
                           specialist — given to every specialist (a
                           relevant Jira ticket isn't domain-specific the
                           way a GitHub code fix is).
py/scripts/
  run_listener.py          main entry point
  run_live_test.py         manual single-incident run (old deterministic
                           Edge UI pipeline)
  replay_alert.py          manual single-incident run through the REAL
                           router + specialist dispatch (handler.handle_alert)
  post_decision.py         post an investigated thread from a decision JSON
  check_setup.py           preflight — checks every specialist's MCP server
                           and case library individually
fixtures/
  alerts-devops-real.json  real captured alert cards — the parser regression set
```

`DESIGN.md` is the discovery document behind all of this: current-state
findings from `#alerts-devops` and `#comms-noc`, the per-alert-type
automation matrix, guardrails, and the rollout plan. Read it before extending
the agent to a new alert type.

## Current gaps

- **No read credential for the daemon.** The bot token lacks
  `channels:history`, so unattended runs cannot read either channel. Either
  grant that scope to the bot app, or set `SLACK_USER_TOKEN` — the latter also
  unlocks search and the tool-driven investigation. Until then use
  `post_decision.py` with an interactive Claude session, which needs neither.
- **No alert type is mapped to Grafana panels yet.** The capture machinery is
  verified working, but `config/panel_map.json` is empty until someone
  confirms which dashboard belongs to which alert. See its `_comment` for the
  candidate list.
- **Only engine-failure rate has a deterministic evidence pipeline.** Every
  other alert type gets case-library + Grafana triage. DESIGN.md §3 ranks
  which ones are worth building next.
- **No action-execution layer.** Approve/Deny buttons record a decision and
  nothing else, by design (§5).
- **`RECENT_CHANGES` has no source.** The triage prompt asks for it and is
  told explicitly that the feed is empty, so the model does not invent one.


claude --resume 443cdf67-0d26-44ce-b043-45e7976d4ecb
claude --resume 1e8d7155-6240-4238-ad31-1323ad217a19
