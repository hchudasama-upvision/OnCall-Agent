# CLAUDE.md — OnCall-Agent

Read `README.md` for what this is and how to run it, and `DESIGN.md` for the
discovery findings behind every design choice. This file records the
decisions and constraints that are **not** visible in the code.

## Non-negotiables

1. **Deterministic code decides; the model writes.** Which alerts trigger,
   which route they take, and which evidence is attached are all decided in
   code. The model produces narrative, hypotheses and an escalation
   recommendation. Never move evidence selection, routing, severity or
   paging into the model — DESIGN.md §5.
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
5. **Safety defaults stay defaults.** `POST_MODE=dry_run` and
   `TRIGGER_ON=victorops`. Do not flip either as a convenience.

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
- **`config/panel_map.json` holds only verified entries.** A wrong panel id
  renders a real-looking picture of the wrong graph — worse than no evidence.
  thanos-grafana carries many near-identical per-cluster dashboard copies, so
  everything except the engine-failure dashboard is deliberately unmapped
  until someone who knows the cluster confirms it.

## Traps already hit — do not re-introduce

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
