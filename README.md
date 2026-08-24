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
                      │        + mapped Grafana panels    + #alerts-devops
                      │                    │                correlation
                      │                    │              + mapped Grafana panels
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

**Deterministic code decides; the model writes.** Which alerts trigger, which
route they take, and which evidence gets attached are all decided in code.
The model produces narrative, hypotheses, and an escalation recommendation —
and never sees a URL it may invent, never references a screenshot that was
not actually captured, and never executes anything.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r py/requirements.txt
playwright install chromium && playwright install-deps   # install-deps needs sudo

cp .env.example .env        # then fill it in
python py/scripts/check_setup.py
```

`check_setup.py` reports PASS / WARN / FAIL per dependency. Almost everything
degrades rather than breaks — no Grafana means no graphs but a full triage;
no `channels:history` means no past-thread grounding but the offline case
library still applies. Only Slack read access is genuinely required.

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
| `config/suppression.json` | chronic fingerprints to never investigate |
| `data/cases.json` | 10 sanitized cases from ~30 real incident threads |

### Adding Grafana panels for an alert type

Panel ids are discovered, never guessed — a wrong id renders a real-looking
picture of the wrong graph.

```bash
python -m oncall_agent.grafana --check                    # reachability, token, renderer
python -m oncall_agent.grafana --list-dashboards engine
python -m oncall_agent.grafana --list-panels <dashboard_uid>
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
> authenticated with the same service-account token. That path needs
> `playwright install-deps`. Installing the renderer plugin server-side would
> make the faster `/render` path light up with no code change.

## Repository layout

```
py/oncall_agent/
  listener.py              watch #alerts-devops (socket mode or polling)
  alert_parser.py          the five #alerts-devops bot formats → ParsedAlert
  handler.py               dedupe, suppress, route, post
  config.py                .env → AgentConfig
  engine_failure_pipeline.py   the Edge UI evidence pipeline
  live_edge_ui_client.py   Edge UI task evidence (stats, failed task, org)
  edge_api.py / edge_environments.py / engine_task_stats.py
  screenshot.py            Playwright capture of Edge UI + log downloads
  decide_resolution.py     schema-validated, guardrailed decision for engine failures
  triage.py                the 7-section report for every other alert type
  llm.py                   claude_cli (default) / anthropic
  case_library.py          data/cases.json matching
  slack_history.py         read #comms-noc resolutions + #alerts-devops context
  slack_post.py            post the decided thread
  slack_blocks.py          Slack size limits and block shaping
  grafana.py               Grafana API: search, panels, render, discovery CLI
  grafana_capture.py       headless-browser panel capture (renderer fallback)
  evidence_panels.py       panel map → PNGs → thread
py/scripts/
  run_listener.py          main entry point
  run_live_test.py         manual single-incident run
  check_setup.py           preflight
src/                       legacy TypeScript implementation, superseded by py/
```

`DESIGN.md` is the discovery document behind all of this: current-state
findings from `#alerts-devops` and `#comms-noc`, the per-alert-type
automation matrix, guardrails, and the rollout plan. Read it before extending
the agent to a new alert type.

## Current gaps

- **`channels:history` is not granted** on the bot token, so past `#comms-noc`
  threads cannot be read yet — the strongest grounding signal the agent has.
  The offline case library covers for it in the meantime. Granting the scope
  (and inviting the app if the channel is private) is a Slack-admin action.
- **Only engine-failure rate has a deterministic evidence pipeline.** Every
  other alert type gets case-library + Grafana triage. DESIGN.md §3 ranks
  which ones are worth building next.
- **No action-execution layer.** Approve/Deny buttons record a decision and
  nothing else, by design (§5).
- **`RECENT_CHANGES` has no source.** The triage prompt asks for it and is
  told explicitly that the feed is empty, so the model does not invent one.
