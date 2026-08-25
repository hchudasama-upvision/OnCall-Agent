# CLAUDE.md — OnCall-Agent

Read `README.md` for what this is and how to run it, and `DESIGN.md` for the
discovery findings behind every design choice. This file records the
decisions and constraints that are **not** visible in the code.

## Non-negotiables

1. **Code decides WHETHER to act; the model decides WHAT to gather.**
   Revised by the owner on 2026-08-24, and the split matters:

   Code owns — what triggers (VictorOps incidents only), suppression, dedupe,
   route selection, whether anything is posted at all, and every guardrail in
   §2. Those stay deterministic.

   The model owns — the live investigation. It searches Grafana, reads what a
   panel actually queries, runs PromQL to translate what the alert gives into
   what the panel needs, renders, and retries when a render comes back empty.

   The first version encoded all of that into config/panel_map.json:
   dashboard uid, panel ids, which label index fills which variable, a PromQL
   lookup for hostname->instance. It worked, and it was wrong — every new
   alert type needed a human to re-derive it by hand, and a variable that
   silently matched nothing produced a green "N/A" gauge nobody caught until
   it was in the incident thread. Tools give the model the feedback loop a
   human has. `data/cases.json` says WHAT to do for an alert type;
   `panel_map.json` is now only a hint for dashboards already pinned.

   Still never the model's: paging, severity, or executing anything.
2. **Never fabricate.** `decide_resolution.py` refuses to post any URL that is
   not the incident's own real permalink, and any `evidence_keys` value that
   is not a file actually captured. Violations abort loudly rather than being
   silently sanitized: a quietly "fixed" bad decision is worse than a visible
   failure. Keep that property when adding evidence types.
3. **No action execution.** There is no remediation layer. Approve/Deny
   record a decision and strip themselves. When an executor is built, it
   validates typed actions with guardrails in code — never from model output.
4. **Credentials only from `.env`.** Every tool reads its own credential from
   env. No credential ever enters a prompt.
   The `.env` here is INHERITED from the prototype's author, not issued to
   this agent: the Slack bot lives in someone else's workspace, and
   `EDGE_USERNAME` is a named person — so every Edge UI screenshot the agent
   posts shows that person's logged-in session. Fine while testing, not fine
   in an incident thread. Dedicated identities are Phase 0 in DESIGN.md §5;
   `config/slack_app_manifest.yaml` covers the Slack half.
   Note Slack fixes a token's scopes at INSTALL time — adding scopes to the
   manifest does nothing until the app is reinstalled, which is why the
   inherited token has only chat:write + files:write despite a manifest that
   declares channels:history.
5. **Safety defaults stay defaults.** `POST_MODE=dry_run`,
   `TRIGGER_ON=victorops`, and `PROFILE=test`. Do not flip any of them as a
   convenience. In particular `PROFILE=test` must never grow channel
   defaults: it once fell back to the real Veritone channels, so an .env
   missing `ALERTS_CHANNEL` pointed at the live NOC channel and `--live`
   would have posted into it. The test profile refuses to start instead.

## History

Three lineages merged here:

- **This repo, before 2026-08-24** — a TypeScript engine-failure pipeline
  (`src/`), rewritten in Python under `py/`. The TS tree is superseded and
  left in place; do not extend it.
- **The 2026-08-24 architecture pivot** — root-cause narrative, escalation
  routing and message structure moved out of a hardcoded template
  (`composer.ts`, `suggestOwningTeam`) into an LLM decision step grounded in
  real past `#comms-noc` resolutions. Evidence gathering stayed fully
  deterministic.
- **`~/work/noc-ai-lab`** — a sanitized-replay prototype in a personal Slack
  workspace. Ported in: the case library, the 7-section triage prompt, the
  Grafana evidence tool, panel-map key normalization, and the Slack
  size-limit handling. Its `bot.py`/`replayer.py` were not ported; the
  listener here reads the real `#alerts-devops` instead of a replayer.

## Decisions made deliberately

- **`gemini` was not ported.** The prototype ran on strictly sanitized replay
  data, where a free tier that may train on inputs was an acceptable trade.
  This repo handles real org names, task ids, error payloads and internal
  hostnames. Re-adding it needs a paid no-training tier and an owner decision.
- **`claude_cli` is the default provider,** matching what
  `decide_resolution.py` already used — one login to keep alive, not two.
  Auth is the CLI's own OAuth session, which is a company Team plan account:
  usage is billed to and visible to the org, and the agent shares the
  interactive rate-limit window. Headless cannot run an OAuth flow, so when
  the refresh token expires this provider fails until `claude` is run
  interactively again.
- **Two transports.** Socket Mode needs an app-level token that only a Slack
  admin can mint, so polling `conversations.history` is the default — it
  works with the bot token the repo already has. Buttons only render in
  Socket Mode: a button nobody is listening for is worse than no button.
- **Channel names.** The brief said `#coms-noc` / `alert-devops`; the real
  channels are `#comms-noc` (C01F810QM96) and `#alerts-devops` (C909ZH4ET).
  `SLACK_CHANNEL` remains "where the agent posts" (the repo's original
  meaning) and takes precedence over `COMMS_CHANNEL`; `HISTORY_CHANNEL` is
  where past resolutions are read from, so posting can be pointed at a test
  channel while grounding still comes from the real one.
- **Grafana is not the default evidence.** Engine-failure-rate alerts are an
  EDGE UI investigation (engine name, task counts, error type, task/job logs)
  — that is what the on-call engineer posts, confirmed against real
  #comms-noc threads. A Grafana entry for that alert was added here once
  because that dashboard was the one that could be *verified to render*, which
  is a different question from whether it is the right evidence. Removed. Ask
  which dashboard belongs to an alert; do not infer it from what renders.
- **`config/panel_map.json` holds only verified entries.** A wrong panel id
  renders a real-looking picture of the wrong graph — worse than no evidence.
  thanos-grafana carries many near-identical per-cluster dashboard copies, so
  everything except the engine-failure dashboard is deliberately unmapped
  until someone who knows the cluster confirms it.

## Traps already hit — do not re-introduce

- **Formats were guessed before the channel was read, and every guess was
  wrong.** Fixed against `fixtures/alerts-devops-real.json`; run that fixture
  set after touching `alert_parser.py`. Specifically: VictorOps puts the
  incident NUMBER in `INCIDENT_NAME` (reading the title from it produced
  threads headed "Alert: 119881") — the title is in the linked heading; the
  metadata block mixes lower-case transmitter keys with upper-case VictorOps
  keys and glues the first to the ``` fence, so an upper-case-only regex
  dropped `monitoring_tool`/`state_message` entirely; the env separator is
  `" : "`, alert lines can carry several `" - "` (`us-1 - prod - Alb…`), and
  PandoLogic lines carry TWO parenthesised groups.
- **Do not rsplit on `" - "` to find the alertname.** `"NOC Health Check -
  Systems Alerting"` is one name; only strip segments that actually look like
  an environment (`_ENV_PREFIX`).
- **The `#comms-noc` header is `Alert:` unbolded with the whole quote bolded,
  linking the VictorOps PORTAL url** — not `*Alert:*` with a Slack permalink.
  Verified against real messages; `compose_top_level_text` now matches byte
  for byte.
- **Most engine-failure incidents auto-resolve before anyone can act**
  (`CURRENT_ALERT_PHASE: RESOLVED`, `RESOLVED_BY: SYSTEM`). `ParsedAlert.
  is_trigger` excludes them; opening a thread on one is pure noise.
- **Headless `claude -p` has no claude.ai connector.** It answers
  `NO_SLACK_TOOLS`. Tools must come from `--mcp-config` pointing at
  `slack_mcp_server.py`. Always pass `--strict-mcp-config`, or the CLI also
  loads whatever MCP servers the invoking user happens to have.
- **Never validate model-emitted URLs by prefix.** With any real host in the
  corpus, `startswith` authorizes an invented path under it. Exact match after
  punctuation-stripping, and nothing else.
- **The MCP server must never gain a write tool.** Reads use the user token,
  writes use the bot token, and that split is what stops a prompt-injected
  alert payload from posting. `investigate.py` allow-lists tool names as the
  second lock.
- **The package is under `py/`, so `python -m oncall_agent.…` needs either
  `pip install -e .` or `PYTHONPATH=py`.** The `.mcp.json` shipped in the repo
  says `"command": "python"`, which only works from an activated venv;
  `investigate.py` rewrites it at run time to `sys.executable` with an absolute
  cwd (`.state/mcp-resolved.json`) so the server always starts under an
  interpreter that has slack_sdk. A server that fails to start does not error —
  the tools just silently do not appear.
- **stdout of the MCP server is protocol only.** Every diagnostic goes to
  stderr; one stray `print` corrupts the stream and the tools vanish.

- **Never screenshot a Grafana panel on a fixed timer.** `networkidle` plus a
  2.5s settle produced a pristine capture of the word "Loading …" for the
  timeseries panel while the gauge panel beside it rendered fine — panels
  finish their queries well after the page goes idle. `_PANEL_READY_JS` polls
  for actual drawn content (`canvas`/`svg`/`table`, or an explicit "No data")
  and capture RAISES on timeout rather than attaching the spinner.
- **Grafana lies about its renderer.** `thanos-grafana.ops.veritone.com`
  (v12.1.0) has no `grafana-image-renderer` plugin, and answers every
  `/render/` URL with HTTP 200 and a valid PNG reading *"No image renderer
  available/installed"*. The prototype's probe-the-render-endpoint check
  reported a false PASS on it. `renderer_available()` asks `/api/plugins`
  instead, and `render_panel()` rejects any PNG whose dimensions are not the
  ones requested.
- **Slack has two independent size caps.** A section block caps at 3000
  chars; the top-level `text` field caps at 4000 *separately*. The 7-section
  report runs ~5.5k, so `chat.update` was rejected outright and the
  "investigating…" placeholder sat there forever while evidence posted around
  it — the agent looked hung. `slack_blocks.fallback_text()` handles it.
- **Post the placeholder before starting the render thread.** `chat_update`
  keeps the original ts, so the triage stays above the panels that land while
  it runs. Posting it fresh at the end buries it under them.
- **`files_upload_v2` resolving ≠ the file being visible.** A plain text reply
  posted immediately after can land above it; `slack_post.py` sleeps 2s.
- **Panel-map and case-library keys need the same normalization.** Both are
  written the way the alert reads in Slack, so they can be compound
  (`"KubePodsNotReady / KubePodCrashLooping"`) while a fingerprint is one bare
  alertname. A raw dict lookup silently attaches nothing. Any new mapping
  file needs `_split_variants()`-style treatment.
- **The window sanity check must abort, not warn.** Posting text that says
  "15 minutes" beside a screenshot showing a different window is the exact
  drift bug the engine-failure pipeline exists to prevent.
- **Use the stats scraped off the Tasks page screenshot,** not a separately
  timed stats-API call — the two are fetched seconds apart and real task
  counts drift that fast.
- **Bolt drops bot messages by default.** The alerts are posted *by*
  integrations, so `ignoring_self_events_enabled=False` is required. Safe only
  because the handler reacts to `alerts_channel` and writes to
  `comms_channel`; if that ever changes, add a `bot_id` self-check or the
  agent will triage its own output.
- **Label every prompt input, including empty ones.** An omitted section
  invites the model to invent a change record; "never fabricate a permalink"
  only binds if it knows the feed is empty by design. Hence
  `NO_CHANGE_FEED` / `NO_OBSERVATIONS`.

## Style

- Python 3.11+, dependency-light (slack_sdk, slack-bolt, playwright, requests,
  python-dotenv). Small readable modules over frameworks — one ops engineer
  has to be able to read this.
- A daemon must not die on one bad alert: the listener catches per-message,
  and every evidence source degrades to "unavailable, here is why" rather
  than raising.
- After changes: `python py/scripts/check_setup.py`, then
  `python py/scripts/run_listener.py --once` (dry run) and check the printed
  thread.
