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

### Ready-to-run alerts (real hosts/volumes/VMs — these render)

Every host, drive, VM and PVC below was checked against thanos-grafana on
**2026-08-26** and the observed value is noted. The values drift — that is the
point of re-checking, not a reason to distrust the line. Add `--dry-run` to
investigate without posting anything to Slack.

```bash
# 1. Windows disk CRITICAL — genuinely full: E: at 99.98%, 0.27GB free of 1536GB
#    Exercises: the new DiskSpaceUtilization/pandologic-windows case, $host as an
#    instance not a hostname, and the "panel shows every drive" gotcha the hard
#    way — SQL-STG has 13 volumes, so the drive has to be named in the text.
#    Also a SQL* host, so the DB-owner escalation rule applies.
python py/scripts/replay_alert.py --incident \
  "[FIRING:1] PandoLogic - SQL-STG DiskSpaceUtilizationCritical (pandologic 192.168.4.50:9182 windows_exporter critical SQL-STG E: windows_exporter)"

# 2. Windows disk WARNING on the OS drive — C: at 98.31%, 1.60GB free of 94.7GB
#    Exercises: Warning vs Critical (the suffix match must not answer a Warning
#    with the Critical-only SQL entry), and a C: drive — cleanup advice differs
#    from a data volume.
python py/scripts/replay_alert.py --incident \
  "[FIRING:1] PandoLogic - STG-backend14 DiskSpaceUtilizationWarning (pandologic 192.168.4.14:9182 windows_exporter warning STG-backend14 C: windows_exporter)"

# 3. Windows memory high — the honest-negative case: Web30 is the highest of the
#    21 memory-reporting hosts and is only at 42.5%, nowhere near the 95% the
#    alert claims. Exercises: the Zabbix shape (no [FIRING:n], no label group),
#    the hostname -> instance resolve (Web30 -> 10.60.6.30:9182), and whether the
#    agent says "already released, ack and resolve" instead of inventing a cause.
python py/scripts/replay_alert.py --incident \
  "High memory utilization (>95% for 5m) - Web30"

# 4. VMware VM red alarm, MEMORY — ES_logs_K1 genuinely at 88.99%, and its series
#    carries "Last Backup successfully completed at ..." from ~15:16 today.
#    Exercises: picking panel 18 from triggeredAlarm:VmMemoryUsageAlarm, the
#    percent x100 metric (must read 88.99%, not 8899%), and confirming a
#    backup-window spike from the Rubrik_LastBackup label rather than guessing it.
python py/scripts/replay_alert.py --incident \
  "[FIRING:1] PandoLogic - VM ActiveRedAlarms (n/a triggeredAlarm:VmMemoryUsageAlarm RealMatch-Cluster01 RealMatch-Datacenter ny1stg9248_vol_06 pandologic ny1esx9404.verimatch.com vmware_vcenter critical n/a ES_logs_K1)"

# 5. VMware VM yellow alarm, CPU — STG-ESLinux1-22.04 at 113% of its max CPU.
#    Exercises: the CPU branch (panel 17, NOT 18 — that discriminator is the
#    whole point of the entry), a warning-severity alarm, and a value above 100%
#    that the agent should report as-is and flag rather than quietly clamp.
python py/scripts/replay_alert.py --incident \
  "[FIRING:1] PandoLogic - VM ActiveYellowAlarms (n/a triggeredAlarm:VmCPUUsageAlarm RealMatch-Cluster02 RealMatch-Datacenter ny1stg9248_vol_04 pandologic ny1esx9151.verimatch.com vmware_vcenter warning n/a STG-ESLinux1-22.04)"
```

```bash
# 6. PVC filling up — 85.65% used on the aiw-stg198 groundcover clickhouse shard.
#    NOTE: this one routes to the KUBERNETES specialist, not the Grafana one.
#    Both agents carry a copy of this case on purpose (see either entry's
#    _shared_with); this run tests the k8s side plus the shared panel hint.
python py/scripts/replay_alert.py --incident \
  "[FIRING:1] aiw-stg198 : KubePersistentVolumeFillingUp (kubelet groundcover data-groundcover-clickhouse-shard0-0 aiw-stg198 warning)"
```

Older lines that still work, kept for a second host per family: `SVC120 D:`
(25.7% used — the case that turned out to be a rule-threshold problem, not a
disk problem), `SVC182`/`SVC176` for memory, `ApexSQL` for a VM alarm, and
`aiw-prd5001` / `aiw-wpsc01` for PVCs.

### Engine backlog (Edge UI agent — Backlog card + per-engine numbers)

```bash
python py/scripts/replay_alert.py --dry-run --incident \
  "[FIRING:1] aiw-prod1001 : Engine backlog critical for 30m"
```

Numbers come from `/edge/v1/proc/jobs/backlog_count_by_engine` (the endpoint the
Edge UI "Backlog" card draws); the screenshot is that card, clipped. Other
environments with real backlog right now: `aiw-prd5001`, `aiw-uk1001`.

Check the numbers without an investigation:

```bash
../.venv/bin/python -c "from dotenv import load_dotenv; load_dotenv('../.env'); \
from oncall_agent.Agents.Edgeui_Agent import server; \
print(server.tool_fetch_engine_backlog('aiw-prod1001'))"   # run from py/
```

### VMware VM alarm (3 panels + top 5 processes)

```bash
python py/scripts/replay_alert.py --dry-run --incident \
  "[FIRING:1] PandoLogic - VM ActiveRedAlarms (n/a triggeredAlarm:VmCPUUsageAlarm RealMatch-Cluster02 RealMatch-Datacenter ny1stg9248_vol_07 pandologic ny1esx9680.verimatch.com ny1wv5840.verimatch.com vmware_vcenter critical n/a STG-Backend5)"
```

Renders CPU, memory AND disk for the VM and calls `top_processes`. Processes come
from windows_exporter metrics with **no login** where the host has them — try
`SVC182` to see that path work:

```bash
cd py && ../.venv/bin/python -m oncall_agent.Agents.Windows_Agent.server --selftest SVC182
```

STG-Backend5 has no exporter, and its WinRM authenticates but refuses to open a
shell (`0x80070002`), so the process breakdown is reported as unavailable there.
Either enable the remote shell on that VM, or (better) install windows_exporter
on it — that removes the credential from the path entirely.

### VMware ESXi host CPU / memory (ESXi panel, captured periodically)

```bash
FOLLOW_UP_MINUTES=0.3,0.6 python py/scripts/replay_alert.py --dry-run --incident \
  "[FIRING:1] PandoLogic - HostCPUUtilizationCritical (RealMatch-Cluster01 RealMatch-Datacenter ny1esx9679.verimatch.com ny1wv5840.verimatch.com vmware_vcenter critical n/a)"
```

Swap `HostCPUUtilizationCritical` for `HostMemoryUtilizationCritical` to test the
memory variant (panels 18/13 instead of 17/12).

The first FQDN in the label list is the ESXi host and the subject; the second is
the vmware_exporter and appears on every host's series. Both rules live in Thanos
at `> 85` `for 10m`, and their selector is CLUSTER-wide — the re-check scopes it
to the alerting host before quoting any number.

### High concurrent_requests / nodejs_active_handles (Thanos, periodic captures)

```bash
FOLLOW_UP_MINUTES=0.3,0.6 python py/scripts/replay_alert.py --dry-run --incident \
  "aiw:zpfc02 - High concurrent_requests for core-admin-server"
```

The alert title is the Thanos rule name, so the threshold (`> 10`, `for 5m`) and
the selector come from the rule itself. Real rules exist for `core-admin-server`
and `core-graphql-server` in `aiw-zpfc02` and `aiw-wpsc01`; the
`nodejs_active_handles` variant works the same way (note the rule metric is
`nodejs_active_handles_total`).

Check the rule and current value without an investigation:

```bash
.venv/bin/python -c "import sys; sys.path.insert(0,'py'); \
from oncall_agent.Agents.Grafana_Agent import server; \
print(server.tool_thanos_alert_status('High concurrent_requests for core-admin-server'))"
```

### ALB unhealthy hosts (target health + graph, re-checked at +3/+6 min)

```bash
FOLLOW_UP_MINUTES=0.3,0.6 python py/scripts/replay_alert.py --dry-run --incident \
  "[FIRING:1] uk-1 : uk-prod - ALBUnhealthyHostCritical (*Summary:* Application Load Balancer app/uk-prod-fastcore-app-http/992c86a8ba9f5fb3 has at least 1 unhealthy instances for at least 15m)"
```

Needs `aws sso login --profile uk-prod` (account `026972849384`, eu-west-2 — it
shares the `veritone-sso` session with `main`, so one login covers both).

Without the `FOLLOW_UP_MINUTES` override this family re-checks at **+3 and +6
minutes**, not the usual +5/+10. Check target health directly:

```bash
.venv/bin/python -c "import sys; sys.path.insert(0,'py'); \
from dotenv import load_dotenv; load_dotenv('.env'); \
from oncall_agent.Agents.AWS_Agent import server; \
print(server.tool_alb_target_health('app/uk-prod-fastcore-app-http/992c86a8ba9f5fb3'))"
```

### Rekognition throttling (GovCloud — repeated captures at +5/+10)

```bash
FOLLOW_UP_MINUTES=0.3,0.6 python py/scripts/replay_alert.py --dry-run --incident \
  "Rekognition-ThrottledCount-High-wpsc01"
```

GovCloud account `113765098011` via profile `us-1-gov` (static keys assuming a
role — **not** SSO, so `aws sso login` is the wrong advice for it).

CloudWatch's own image API is unusable in that account (`Throttling: Rate
exceeded`, always), so graphs are drawn locally from the same datapoints and the
caption says so. The metric is silent between bursts; when the 24h window has no
datapoints the re-check widens the graph to 168h and states that.

### RDS alerts (AWS specialist — two graphs, top SQL, +5min usage re-check)

Needs a live SSO session: `aws sso login --profile main`.

```bash
# The real prod core database — the agent resolves prod-core-rds from the alert
python py/scripts/replay_alert.py --dry-run --incident \
  "[FIRING:1] us-1 : prod - RDS_CPUUtilizationAvgCriticalCore"

# Point the +5min re-check at the stage instance instead, and compress the wait
FOLLOW_UP_MINUTES=0.3 FOLLOW_UP_RDS_INSTANCE=stage-core-rds2 \
  python py/scripts/replay_alert.py --dry-run --incident \
  "[FIRING:1] us-1 : prod - RDS_CPUUtilizationAvgCriticalCore"
```

Exercise the tools directly (stderr; stdout is MCP protocol):

```bash
cd py && ../.venv/bin/python -m oncall_agent.Agents.AWS_Agent.server --selftest stage-core-rds2
```

`AWS_PROFILES` in `.env` bounds which accounts are readable (`main` today);
`AWS_REGIONS` bounds the regions. If a tool reports the session expired, that is
what it means — nothing is estimated.

### EndpointDown (names the endpoint; the action stays a human's)

```bash
python py/scripts/replay_alert.py --dry-run --incident "Alert: ops-prom : EndpointDown"
```

The thread gets the endpoints, each one's `status` versus its expected code, TLS
state, and whatever the change/maintenance channels say. Credential-bearing
query values in a probed URL are stripped before anything is posted.

The change check needs `CHANGE_CHANNELS` in `.env` (e.g.
`CHANGE_CHANNELS=#prod-deploy,#change-mgmt`) **and** `channels:history` on the
token. Until both are in place it reports the check as *unavailable* — which is
deliberate: "no announcement found" and "could not look" must never read alike.

See what is down right now without running an investigation:

```bash
cd py && ../.venv/bin/python -c "from dotenv import load_dotenv; load_dotenv('../.env'); \
from oncall_agent.Agents.Grafana_Agent import server; print(server.tool_endpoint_status())"
```

### API response-code alerts (with the automatic 5/10-minute re-check)

```bash
# The first post carries the response-codes graph + the real success rate; a
# re-check then posts recovery into the SAME thread 5 and 10 minutes later.
python py/scripts/replay_alert.py --dry-run --incident \
  "US-Prod Response Codes - nginx -ai13s   aiWARE/prod"

# Same thing without the wait — compress the re-check to ~12s for testing:
FOLLOW_UP_MINUTES=0.2,0.4 python py/scripts/replay_alert.py --dry-run --incident \
  "US-Prod Response Codes - nginx -ai13s   aiWARE/prod"
```

Other environments on the same dashboard (all verified to resolve): `US-Prod
Response Codes - haproxy`, `UK-Prod Response Codes - nginx`, `Azure Prod
Response Codes - nginx -ai13s`, `Azure Stage Response Codes - nginx -ai13s`,
`DMH CrUX - API Response Codes`, `DMH Core - API Response Codes`.

Check the panel pairing and the current rate without running an investigation:

```bash
cd py && ../.venv/bin/python -m oncall_agent.Agents.Grafana_Agent.api_health \
  "US-Prod Response Codes - nginx -ai13s"
```

Pending re-checks live in `.state/followups/` and are resumed on listener
start; delete that directory to cancel them.

### Kubernetes alerts (real pods on the staging cluster)

These take the Kubernetes specialist, which as of 2026-08-27 has read-only
kubectl against `~/eks/stage/aiw-stg198`. The thread carries three things: the
pod's state, the log line that proves why it died, and whether its replica
peers are running.

```bash
# A real crashlooper on aiw-stg198 — Deployment/discovery-app-stg198, 1 replica,
# 0 ready. Exercises get_pod_state + get_pod_logs(previous=true) + the peer walk.
python py/scripts/replay_alert.py --dry-run --incident \
  "[FIRING:1] aiw-stg198 : KubePodCrashLooping (kubelet aiware discovery-app-stg198-5cf8bdc484-4l4mz aiw-stg198 warning)"

# The alert shape that names no pod at all — the agent has to find them itself.
python py/scripts/replay_alert.py --dry-run --incident \
  "[FIRING:3] aiw-stg198 : KubePodsNotReady (kubelet aiware aiw-stg198 warning)"
```

Pod names churn. Get current ones with:

```bash
.venv/bin/python -m oncall_agent.Agents.K8S_Agent.server --selftest aiw-stg198
```

(run it from `py/`, or with `PYTHONPATH=py`. Output goes to stderr — stdout is
MCP protocol.) If it reports the cluster unreachable, the AWS session behind
that kubeconfig has expired and a human has to refresh it; `python
py/scripts/check_setup.py` shows the same thing as `kube api`.

### Run the whole set in one command

Sequential, nothing posted to Slack — ~12 minutes and roughly $2.50 for all five.
Drop `--dry-run` to post each one into `slack-alerts` with its thread.

```bash
cd ~/work/OnCall-Agent
for a in \
  "[FIRING:1] PandoLogic - SQL-STG DiskSpaceUtilizationCritical (pandologic 192.168.4.50:9182 windows_exporter critical SQL-STG E: windows_exporter)" \
  "[FIRING:1] PandoLogic - STG-backend14 DiskSpaceUtilizationWarning (pandologic 192.168.4.14:9182 windows_exporter warning STG-backend14 C: windows_exporter)" \
  "High memory utilization (>95% for 5m) - Web30" \
  "[FIRING:1] PandoLogic - VM ActiveRedAlarms (n/a triggeredAlarm:VmMemoryUsageAlarm RealMatch-Cluster01 RealMatch-Datacenter ny1stg9248_vol_06 pandologic ny1esx9404.verimatch.com vmware_vcenter critical n/a ES_logs_K1)" \
  "[FIRING:1] PandoLogic - VM ActiveYellowAlarms (n/a triggeredAlarm:VmCPUUsageAlarm RealMatch-Cluster02 RealMatch-Datacenter ny1stg9248_vol_04 pandologic ny1esx9151.verimatch.com vmware_vcenter warning n/a STG-ESLinux1-22.04)" \
; do
  echo; echo "================ $a"
  python py/scripts/replay_alert.py --dry-run --incident "$a"
done 2>&1 | tee /tmp/grafana-agent-test.log
```

All five at once instead (each gets its own evidence dir and incident number, so
they cannot clobber each other) — same lines, `&` and a `wait`:

```bash
for a in "<line1>" "<line2>" ...; do
  python py/scripts/replay_alert.py --dry-run --incident "$a" > "/tmp/$(date +%s%N).log" 2>&1 &
done; wait
```

Replays write agent memory. Each run records what it concluded under
`.state/memory/<specialist>/`, keyed by alert type, and the next run of the same
alert reads it back — including the replay's synthetic incident number, which the
agent will then cite as a prior incident ("this same alert on incident 994522").
That is the feature working, but it is test residue: clear it between rounds, or
before the listener runs for real.

```bash
rm -rf .state/memory        # drop everything the replays remembered
```

What each run should get right, and what to look for when reviewing the thread:

| # | Should say | Red flag |
|---|---|---|
| 1 | E: named explicitly, % **and** free GB, DB owner involved | a panel posted with no drive named — 13 volumes on that graph |
| 2 | C: at ~98%, distinguishes OS drive from data volume | matching the SQL-only case and asking the DB team |
| 3 | "not high right now / already released", ack + resolve | any invented cause, or an empty panel attached |
| 4 | 88.99% (not 8899%), backup window cited from the label | a rendered CPU panel instead of memory |
| 5 | panel 17 (CPU), >100% reported and flagged as odd | silently reporting 113% as if it were normal, or panel 18 |

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
