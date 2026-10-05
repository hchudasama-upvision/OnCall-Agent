---
name: daily-update
description: Scan #alerts-devops and #comms-noc for what changed, and update the case inventories from it — new alert types, new payload fields, changed resolution methods. Run daily or weekly. Use when asked to refresh/update the case library, catch up on alerts, or check whether the inventory has fallen behind the real channel.
---

# daily-update — keep the case inventories level with reality

The case libraries go stale in two independent ways, and this skill covers both:

1. **The alerts change.** New alert types appear, existing ones gain or lose
   labels, and the Alertmanager→VictorOps template itself changes (it gained
   the whole `*Summary:*`/`*Description:*`/`*Labels:*` block between
   2026-08-24 and 2026-09-11 and nothing here noticed for weeks).
2. **The method changes.** The NOC engineers change how they handle an alert.
   That only ever shows up in `#comms-noc` threads, never in the alert itself.

## Why this is a skill and not a daemon

The scan needs to read `veritone.slack.com`. The repo's own bot token **cannot**
— it answers `missing_scope` on both channels, and it belongs to a different
workspace (see CLAUDE.md non-negotiable #4). Headless `claude -p` has no
claude.ai connector either. **You**, running in Claude Code, are the only thing
in this system that can read those channels, which is why the procedure lives
here as instructions to you rather than in a cron job.

## Channels

| Channel | ID | What to take from it |
|---|---|---|
| `#alerts-devops` | `C909ZH4ET` | the alert PAYLOADS — types, labels, fields, timings |
| `#comms-noc` | `C01F810QM96` | the METHOD — what the on-call engineer actually did |

## Step 1 — find where the last scan stopped

Read `.state/curator/last_scan.json` (it may not exist; that is a first run).
It holds `{"alerts_devops_ts": "...", "comms_noc_ts": "...", "scanned_at": "..."}`.
Scan back only as far as those timestamps. On a first run, one day of
`#alerts-devops` is plenty — it repeats heavily.

## Step 2 — scan #alerts-devops, and SAVE IT VERBATIM

```
mcp__claude_ai_Slack__slack_read_channel(channel_id="C909ZH4ET", limit=40,
                                         response_format="full", cursor=<next page>)
```

- `response_format="full"` is **required**. The `concise` format returns empty
  bodies for these messages — the content is all in attachments.
- Page with the returned cursor until you pass the last-scan timestamp.
- **Append each tool result to a scratch file exactly as returned. Do not
  retype, summarise or "clean up" a card.** A paraphrased label name becomes a
  fabricated one, and the whole value of this scan is that every field traces
  to real bytes. The parser reads the connector's own dump format directly.
- The connector truncates an attachment near 1KB, so a card with many
  sub-alerts arrives cut off. That is fine and expected — the parser flags the
  cut block `truncated`. Never fill in what was cut.

Only VictorOps cards carry the rich payload. Messages from the Alertmanager bot
(`B015LL65HR8`) show a title only; they still count for frequency, but they will
not yield labels.

## Step 3 — run the deterministic gap analysis

```bash
python py/scripts/inventory_gaps.py --alerts <scratch>/scan.txt
```

It parses every card with the agent's own `alert_parser` (the same code the
live pipeline uses) and reports, per alert type:

- `UNROUTED` — no specialist owns it, so it falls to the generalist
- `NO CASE` — routed, but no case entry matches
- `NO alert_payload BLOCK` — its cards carry Summary/Description/Labels and
  the case does not describe them, so the fields go unused
- `UNDOCUMENTED LABELS` — labels observed that the case's block never mentions
- `RESOLUTION TIME DRIFT` — observed Started→Resolved vs `typical_resolution_minutes`

Do not eyeball the cards for this. The script is the source of truth for what
is missing; your job is what to write about it.

## Step 4 — scan #comms-noc for method changes

```
mcp__claude_ai_Slack__slack_read_channel(channel_id="C01F810QM96", limit=40,
                                         response_format="full")
```

Save verbatim too, then `--comms <scratch>/comms.txt` lists the threads worth
reading. For each thread on an alert type you have a case for:

```
mcp__claude_ai_Slack__slack_read_thread(channel_id="C01F810QM96", message_ts="<ts>",
                                        response_format="full")
```

Compare what the engineer actually did against that case's `diagnosis_steps`
and `resolution_steps`. You are looking for:

- a **different first move** than the case prescribes
- a tool, dashboard, query or command the case does not mention
- a conclusion the case says is impossible, or vice versa
- an escalation that went somewhere new

**Threads in `#alerts-devops` are worthless for this** — every reply there is
VictorOps ACK/RESOLVE bookkeeping. The diagnosis only ever lives in
`#comms-noc`.

## Step 5 — propose the changes, then apply them

Show the user a short list of proposed edits **before** writing, grouped by
file, each with the incident number it came from. Then apply:

- **`Agents/<Name>/data/cases.json`** — the main output. Add or extend
  `alert_payload` (what Summary and Description are shaped like, every label
  name, `use_it_for`, and the trap), correct `diagnosis_steps` that now
  describe discovering something the payload hands over, update
  `occurrences_observed` and `typical_resolution_minutes` from the real
  Started/Resolved stamps, and add a `known_root_causes`/`known_causes` entry
  when a thread diagnosed one.
- **`config/specialist_routing.json`** — a keyword for any `UNROUTED` type.
  Routes are matched in file order and first hit wins; put a broad keyword last.
- **`fixtures/alerts-devops-state-message.json`** — add a verbatim card for any
  alert type that has no fixture yet, especially a NEW shape. Mark
  `_truncated_by_connector` honestly.
- **`CLAUDE.md`** — only for a finding that changes how someone would build on
  this: a template change, a new payload trap, a method the NOC abandoned.
- **A new case entry** for a genuinely new alert type, following the shape of
  the entries already there.

## Rules that are not negotiable

- **Only what a real card or thread said.** Every claim carries its incident
  number, and a `source_permalink` where a thread is the source. If you did not
  read it this run, do not write it.
- **The label set is not a schema.** It varies between alert types *and*
  between sub-alerts of the same alert — `KubePodsNotReady` carries
  `owner_kind` on `#121443` and not on `#121450`. Write "labels observed
  include", never "the labels are", and never record a label as absent.
- **Do not delete curated knowledge to make room.** These files hold findings
  from real incidents going back months. Extend an entry; only correct
  something you can show is now wrong, and say why in the entry.
- **Do not touch `.env`, credentials, or `POST_MODE`/`TRIGGER_ON`/`PROFILE`.**
- **Do not post anything to Slack.** This skill reads.

## Step 6 — verify and record

```bash
python py/scripts/test_offline.py       # must stay green
python py/scripts/check_setup.py        # every case file must still load
```

Write `.state/curator/last_scan.json` with the newest `Message TS` seen in each
channel and the run date, so tomorrow's run starts where this one stopped.

Finish by giving the user, per alert type whose case changed, the exact replay
command — the repo's standing convention:

```bash
python py/scripts/replay_alert.py --dry-run --incident "<the alert line>"
python py/scripts/replay_alert.py --dry-run --fixture "<fixture substring>"
```

## Cadence

Daily is comfortable (one day of `#alerts-devops` is a handful of pages).
Weekly is fine too, but expect more pages and prefer scanning `#comms-noc`
first — a week of alerts repeats, while a week of threads is where the real
change is.
