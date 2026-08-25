# Runbook

Every command assumes you are in the repo root. The venv is auto-detected —
you do **not** need to activate it.

```bash
cd ~/work/OnCall-Agent
```

---

## 0. One-time setup

```bash
python -m venv .venv
.venv/bin/pip install -r py/requirements.txt
.venv/bin/pip install -e .
.venv/bin/playwright install chromium
```

---

## 1. Check everything is wired

```bash
python py/scripts/test_offline.py     # 68 checks, no credentials, ~2s
python py/scripts/check_setup.py      # what this machine can actually reach
```

`check_setup.py` is the one to read. Everything should be PASS except the
Slack read line (see §4).

---

## 2. Fire an alert at it  ← **this is the main one**

One command does the whole thing: posts the alert to Slack, investigates it,
and posts the Grafana screenshots into that alert's thread.

```bash
python py/scripts/replay_alert.py --incident "<the alert line>"
```

Takes 2-3 minutes (it is really searching Grafana and running queries).

### Ready-to-run alerts (real hosts/volumes — these render)

```bash
# 1. PVC genuinely under pressure — 85% full, aiw-stg198
python py/scripts/replay_alert.py --incident \
  "[FIRING:1] aiw-stg198 : KubePersistentVolumeFillingUp (kubelet groundcover data-groundcover-clickhouse-shard0-0 aiw-stg198 warning)"

# 2. PVC that has already cleared — 3.8% used, aiw-prd5001
python py/scripts/replay_alert.py --incident \
  "[FIRING:2] aiw-prd5001 : KubePersistentVolumeFillingUp (kubelet aiware aiware-postgres-aiware-njfq-pgdata aiw-prd5001 warning)"

# 3. Windows memory (Zabbix shape — host trails after the dash)
python py/scripts/replay_alert.py --incident \
  "High memory utilization (>95% for 5m) - SVC182"

# 4. Windows disk (PandoLogic shape — labels in parentheses)
python py/scripts/replay_alert.py --incident \
  "[FIRING:1] PandoLogic - SVC120 DiskSpaceUtilizationWarning (pandologic 10.60.4.120:9182 windows_exporter warning SVC120 D: windows_exporter)"

# 5. VMware VM alarm
python py/scripts/replay_alert.py --incident \
  "[FIRING:1] PandoLogic - VM ActiveRedAlarms (n/a triggeredAlarm:VmMemoryUsageAlarm RealMatch-Cluster02 RealMatch-Datacenter ny1stg9248_vol_07 pandologic ny1esx9680.verimatch.com vmware_vcenter critical n/a ApexSQL)"
```

```bash
# 6. PVC 78% — redis on aiw-wpsc01
python py/scripts/replay_alert.py --incident \
  "[FIRING:1] aiw-wpsc01 : KubePersistentVolumeFillingUp (kubelet aiware redis-data-aiware-redis-replicas-0 aiw-wpsc01 warning)"

# 7. Windows memory, second host
python py/scripts/replay_alert.py --incident \
  "High memory utilization (>90% for 10m) - SVC176"

# 8. Windows disk CRITICAL (different host + volume)
python py/scripts/replay_alert.py --incident \
  "[FIRING:1] PandoLogic - ny1wv9601 DiskSpaceUtilizationCritical (pandologic 10.60.254.52:9182 windows_exporter critical ny1wv9601 E: windows_exporter)"

# 9. VMware CPU alarm (yellow)
python py/scripts/replay_alert.py --incident \
  "[FIRING:1] PandoLogic - VM ActiveYellowAlarms (n/a triggeredAlarm:VmCPUUsageAlarm RealMatch-Cluster02 RealMatch-Datacenter ny1stg9248_vol_04 pandologic ny1esx9680.verimatch.com vmware_vcenter warning n/a AdminNet)"
```

### Edge UI alerts (engine failure rate)

These take the OTHER route — the deterministic Edge UI pipeline, no AI
investigation and no Grafana. The agent logs into Edge UI, screenshots the
Tasks and Engine pages filtered to the failing engine, downloads the task and
job logs, and posts those. Engines currently failing, so these return real
data:

```bash
# aiw-prd5001 — SI2 Playback segment creator, ~68% failing
python py/scripts/replay_alert.py --incident \
  "[FIRING:1] aiw-prd5001 : Engine failure rate above 15%"

# aiw-prod1001 — engine named in the alert (this shape is real; see #119827)
python py/scripts/replay_alert.py --incident \
  "[FIRING:1] aiw-prod1001 : Engine failure rate above 15% - WideOrbit Traffic"

# aiw-prod1001 — auto-picks the worst, currently Podcast Adapter (172 failures)
python py/scripts/replay_alert.py --incident \
  "[FIRING:1] aiw-prod1001 : Engine failure rate above 15%"

# aiw-uk1001 — TV and Radio Adapter V3
python py/scripts/replay_alert.py --incident \
  "[FIRING:1] aiw-uk1001 : Engine failure rate above 15%"
```

The engine is chosen by the pipeline: whichever one the alert text names, else
the one with the most failures right now.

Slower than the Grafana alerts (~2-4 min) because it drives a real browser
session against Edge UI, and it needs whatever VPN reaches
`processing.*.aiware.run`.

### Running several at once

Yes — open a terminal per alert and run them simultaneously. Each run gets its
own evidence directory (`dist/evidence/runs/<incident>-<pid>/`) and its own
incident number, so they cannot overwrite each other's screenshots or audit
records.

They all post into the same channel, each as its own alert with its own
thread. Expect 2-3 minutes each and roughly $0.50-$0.90 per alert.

**These post to Slack.** Each one puts the alert card in `slack-alerts`, then
posts the agent's findings and Grafana screenshots as replies **in that
alert's own thread**. The PNGs are also saved to `dist/evidence/`.

To investigate WITHOUT touching Slack, add `--dry-run`.

### Other useful flags

```bash
--labels        # show the alert's parsed fields + which panel matches, then exit
--list          # list the captured real alert fixtures
--fixture "pvc" # replay a captured real alert instead of typing one
```

---

## 3. Run it continuously

Watches `slack-alerts` and investigates each new VictorOps incident by itself.

```bash
python py/scripts/run_listener.py            # dry run — prints, posts nothing
python py/scripts/run_listener.py --live     # posts into each alert's thread

# background
nohup python py/scripts/run_listener.py --live > agent.log 2>&1 &
tail -f agent.log
```

**This needs the Slack read scope — see §4.** Until then use §2.

---

## 4. The one thing still blocking §3

```
FAIL  #alerts-devops read   missing_scope — bot token lacks channels:history
```

The token in `.env` can post but not read. Slack fixes a token's scopes when
the app is **installed**, so this cannot be granted after the fact:

1. https://api.slack.com/apps → the app → **Install App**
2. **Reinstall to Workspace**
3. Copy the new `xoxb-...` and replace the `SLACK_BOT_TOKEN` line in `.env`
4. `python py/scripts/check_setup.py` — that line flips to PASS

`config/slack_app_manifest.yaml` already declares `channels:history`, so the
reissued token comes back with it. Nothing else changes.

---

## 5. Switching to production

```bash
# .env
PROFILE=production      # -> Veritone #alerts-devops + #comms-noc
```

Requires a token issued in the **Veritone** workspace — a test-workspace token
cannot reach those channels no matter its scopes. `check_setup.py` prints a
banner when the profile is production.

---

## What it does and does not do

- **Reads** the alert, searches Grafana live, queries Prometheus, renders the
  right panel, reads past `#comms-noc` threads, posts its findings.
- **Never changes anything.** No restarts, no deletes, no scaling. It holds no
  cluster credential and has no tool that could.
- When a change *is* needed it posts a **proposal** with Approve/Deny. The
  buttons record the decision; a human still runs the command.
- Engine-failure alerts are untouched — they go to the existing Edge UI
  pipeline (`py/scripts/run_live_test.py`), no AI involved.
