# On-Call Alert Automation — Discovery Findings & High-Level Design

**Author:** SRE / Automation Architecture (AI-assisted discovery)
**Date:** 21 August 2026
**Status:** Draft for stakeholder review
**Sources reviewed:** Slack `#alerts-devops` (C909ZH4ET) and `#comms-noc` (C01F810QM96) message history; Confluence DEV space runbooks including *Runbook: On-Call* (master index, 17 child runbooks), *Prod Issues / Defcons / Alerts*, *Runbook: Engine failure rate over 15%*, *Runbook: PandoLogic Alerts*, and *NOC – Infrastructure Monitoring Status – Automation* (master runbook for the existing NOC-AUTOMATION bot).

> **Naming note (flagged, not guessed):** the brief referred to `#coms-noc`. No channel by that name exists; the incident-communication channel is **`#comms-noc`**. This document uses the actual name throughout.

> **Scope refinement (v1.1, per stakeholder direction):** the agent's **trigger is VictorOps incidents only** — the paged alerts that carry an incident #, escalation policy, and ack state. Raw Alertmanager warnings, PandoLogic warnings, and Jenkins smoke-test posts in `#alerts-devops` are **not** triggers; they are retained purely as *correlation context* the agent can consult while investigating a VictorOps incident. The agent's output must replicate the current NOC evidence pattern **including visual evidence (screenshots/graph renders)** posted to the incident thread, as done manually today (reference behavior: incident #119679). Per-alert step-wise resolution procedures (SOPs) will be supplied by the NOC team where Confluence runbooks are insufficient; the agent executes only from these curated SOPs (see Appendix B intake template).

---

## 1. Current-State Summary

### 1.1 How alerts arrive

`#alerts-devops` is effectively 100% bot traffic, from five distinct posting sources:

1. **Central Prometheus/Thanos Alertmanager** (`thanos-alertmanager.ops.veritone.com`) — the highest-volume source. Format: `[FIRING:N] <env> - <AlertName> (<link>)`. Covers Kubernetes alerts (`KubePodCrashLooping`, `KubePodNotReady`, `KubeHpaMaxedOut`, `KubePersistentVolumeFillingUp`), platform synthetics (`SiteErrorStatusCodes`, `AlbUnhealthyHostWarning/Critical`, `Confluence Platform`, `Qualified Platform`, `Pantheon Platform`), and self-monitoring (`ThanosReceiveForwardFailures`, `ThanosComponentHighMemoryUsage`). Observed volume: dozens per hour; many repeat every 5 minutes for the same unresolved condition (e.g., `aiw-stg198 KubePodCrashLooping` held at FIRING:43–55 continuously through the sampled window and appears identically a month earlier).
2. **PandoLogic local Alertmanager** (`10.60.4.245:9093`) — Windows/VMware estate: `SQL40/SQL41 DiskSpaceUtilizationWarning`, `SQL Server High_XTP_Controller_DLC_Fetch_Latency`, `VM/Host ActiveYellowAlarms` (VmMemoryUsage / HostCPUUsage). **Duplication observed:** the same PandoLogic alert frequently posts twice within seconds — once from the local Alertmanager and once relayed through central Thanos.
3. **VictorOps / Splunk On-Call** (org `wazee-digital-inc`) — the *paging* layer. Posts incident cards with rich metadata (`INCIDENT_NAME`, `ACKED_BY`, `CURRENT_ALERT_PHASE`, contact group e.g. `devops-oncall`, `defcon-dmh`) and threads `ACKED`/`RESOLVED` updates. VictorOps ingests from Alertmanager, Runscope, Email (Imperva cert notices, Zabbix `High memory utilization` on REALMATCH hosts), CloudWatch/Deployments (`Deployments/aws-us1-prod/discovery-app`), Rekognition throttling, Azure API success-rate, and threat alerts.
4. **Jenkins Standard Apps Smoke Test** — 2-hourly `UNSTABLE` reports listing pass/fail per app (Illuminate, Redact, …) per environment (us-1, uk-1, us-gov-1/2, ca-1), with retry counts and links to console/test report.
5. **VictorOps on-call rotation notices** (`ON-CALL CHANGE: <user> is ON/OFF for NOC-VT OnCall`).

### 1.2 Acknowledgment pattern

- Only **VictorOps incidents** are acknowledged; raw Alertmanager floods are not individually acked.
- Ack happens **in VictorOps** (portal/mobile), not via Slack emoji or reply; VictorOps then threads *"Incident #N was ACKED … by @user"* under the Slack card.
- Observed ack latency across five sampled incidents (including 04:00–08:00 IST): **11–17 seconds** from incident creation to ack — consistent with a staffed 24/7 follow-the-sun NOC (rotation notices confirm shift-based `NOC-VT OnCall`).
- Many incidents auto-resolve (`RESOLVED_BY: SYSTEM`) when the underlying alert clears.

### 1.3 Communication pattern in #comms-noc

There is **no formal template**; the observed working convention is:

- On-call engineer posts a top-level message: `*Alert:*` + blockquoted VictorOps incident link and title.
- Investigation happens **in the thread**: failure percentage/graph screenshots (Grafana), raw error snippets in code blocks, engine name + ID, downloaded job logs (zip), affected org identification, and finally an `@mention` escalation to the owning engineering team ("FYI^^"). Recurrences of the same incident are appended to the original thread.
- Root cause and fix often land in the same thread (observed: engineer identified a bad `image2pipe` change and reverted via a GitHub PR).
- Separately: **"Starting ITSM-XXXX"** change-window notices (with `@oncall` subteam mention), and the **NOC-AUTOMATION bot's 2-hourly infrastructure status report**, which humans also thread on for triage (e.g., "please review the backlog in dmh cluster").

**Implicit summary fields extracted from practice:** alert/incident name + link · environment · affected engine/service · error type + sample · scope (orgs affected) · owner escalated to · resolution/status. Missing consistently: explicit severity, start/end time, and a closing status line.

### 1.4 Per-alert-type manual flow (trigger → ack → diagnose → act → communicate → resolve)

| Alert type | Trigger | Ack | Diagnose | Act | Communicate | Resolve |
|---|---|---|---|---|---|---|
| **Engine failure rate >15% / 100%** | Alertmanager → VictorOps page | VictorOps ack (~15 s) | Edge UI (Controller) → Processing Tasks → failed tasks → error type/message; helper script `engine_task_stats.sh` (cron on `ops-monitoring1-ops`) | Post evidence; classify error location | `#comms-noc` thread: %, error, engine ID, logs, org scope; @mention Engines / Processing / Data team per runbook decision tree | Owning team fixes (often code change/revert); alert auto-resolves |
| **KubePod\* (CrashLooping / NotReady / PVFillingUp)** | Alertmanager (channel) + VictorOps for paged variants | Only VictorOps variants acked | Cluster dashboards; K8S troubleshooting runbook (separate page) | Case-by-case | Thread on incident when paged | Frequently self-resolves; chronic stg noise never actioned |
| **PandoLogic VM/Host/SQL** | Local + central Alertmanager | VictorOps when routed via email/Zabbix | Grafana (10.60.4.245), VMware console via FortiClient VPN | Per PandoLogic runbook: delete shadow copies >3 days (SQL41 M:), expand volume (linked runbook), or escalate to SRE after 15 min (Host CPU); IMatch: **wait 30 min** (planned Wed restart window) | `#comms-noc` alert post | Manual confirm |
| **Runscope test failures (SOLR reindex, CNBC, Public Stream)** | Runscope → VictorOps | VictorOps ack | Grafana NSQ/backlog, S3 bucket for VidVita files, CloudWatch logs, Qlik | Mostly verification; vendor email escalation (VidVita) if no files | Thread + email CC platform-ops | Test re-passes |
| **API / GraphQL health (5xx, throttling, fatal errors)** | Alertmanager / VictorOps | VictorOps ack | Kibana fatal-error dashboards, API-Calls Grafana, VividCortex, check `#prod-deploy` for recent deploys | Wait 5–10 min for self-heal → cycle fastcore (Jenkins job) → **rollback deploy (consult aiWARE team first)** → SQL `rate_limit.config_token` update for throttling | Thread; escalate via VictorOps to aiWARE | Error rate normalizes |
| **NSQ / engine backlog** | Alertmanager + NOC-AUTOMATION heuristics (6 documented rules) | — | NSQ Grafana; per-topic depth/consumers/requeue/age queries (fully documented in automation master runbook) | Per NSQ Backlog Management runbook | Thread on 2-hourly status post | Backlog drains |
| **Smoke test UNSTABLE** | Jenkins scheduled job | None observed | Console/test-report links; built-in 4× retry already applied | None observed in-channel | None observed | Next run passes |
| **Email-sourced (Imperva cert revalidation, Zabbix memory)** | Email → VictorOps | VictorOps ack | Read email body; check host | Cert: vendor console action by deadline; memory: check trend, follow memory-increase runbook | `#comms-noc` alert post | Manual |
| **Deployments / ITSM change events** | VictorOps deployment incidents; human "Starting ITSM-…" posts | Ack / 👍 reaction | Watch metrics per deployer's note ("roll back if issues") | Human rollback decision | `#comms-noc` | Deploy completes |

### 1.5 Gaps and inconsistencies found (flagged explicitly)

1. **Chronic alert noise with no runbook path:** `aiw-stg198 KubePodCrashLooping` fires every ~5 minutes at FIRING:45± indefinitely (observed identically in July and August). No ack, no thread, no remediation. Any automation must handle "known-chronic" suppression or it will drown.
2. **Duplicate alert paths:** PandoLogic alerts arrive twice (local + central Alertmanager); several conditions arrive three times (Alertmanager channel message + VictorOps incident + NOC-AUTOMATION heuristic). De-duplication is a prerequisite.
3. **Outdated runbooks:** *Prod Issues / Defcons / Alerts* was last edited **2020** and contains stale links; the master *Runbook: On-Call* mixes 2020-era and 2026-era content. Two explicit in-page gaps: UK Queued/Throttled Jobs ("*Need details on what to look at*") and SpeechMatics EM restart ("*need details how to restart EM*").
4. **No runbook found** (in the DEV space pages reviewed) for: Jenkins smoke-test failures, `SiteErrorStatusCodes`, Imperva domain revalidation, threat alerts, `Deployments/*` incidents, Rekognition throttling. These may exist elsewhere — treat as unverified rather than absent — but nothing is linked from the alert payloads.
5. **Only Engine-failure alerts carry a runbook URL in the alert payload.** Every other type requires tribal knowledge to map alert → runbook. This mapping is exactly what an agent needs made explicit.
6. **No template in #comms-noc:** field coverage varies by engineer; severity/start-time/closing-status are usually absent.
7. **Security issue (independent of this project):** the *Runbook: On-Call* page embeds live-looking API/AUTH tokens and session cookies in curl examples, and the automation master runbook names personal SSO-assumed roles (`…/hpatel`). These must be rotated/parameterized before any system — human or agent — treats these pages as executable instructions.
8. **Channel naming:** brief said `#coms-noc`; actual is `#comms-noc`.

---

## 2. Target-State Agent Workflow

The design **extends the existing NOC-AUTOMATION foundation** (which already runs read-only health checks on a documented rulebook) into an event-driven agent. Conceptual end-to-end flow:

```
                        ┌────────────────────────────────────────────────┐
   VictorOps incident ─▶│ 1. INGEST (TRIGGER = VICTOROPS INCIDENT ONLY)  │
   cards in             │ Slack listener + VictorOps API; parse incident │
   #alerts-devops       │ #, title, monitoring_tool, entity, escalation  │
                        │ policy. Alertmanager/Jenkins/PandoLogic posts  │
   (raw warnings ───────│ are ingested as CONTEXT ONLY — never trigger   │
   feed context store)  │ agent action on their own.                     │
                        └───────────────┬────────────────────────────────┘
                                        ▼
                        ┌────────────────────────────────────────────────┐
                        │ 2. DE-DUPLICATE & CORRELATE                    │
                        │ Fingerprint = (alert name, env, entity).       │
                        │ Collapse local/central duplicates, repeat      │
                        │ FIRING:N re-posts, and VictorOps mirrors into  │
                        │ one incident object. Suppress known-chronic    │
                        │ fingerprints (allowlist, reviewed weekly).     │
                        └───────────────┬────────────────────────────────┘
                                        ▼
                        ┌────────────────────────────────────────────────┐
                        │ 3. ACKNOWLEDGE                                 │
                        │ Ack the VictorOps incident via its API within  │
                        │ SLA; thread 🤖 "Investigating — <runbook link>"│
                        │ on the Slack card so humans see ownership.     │
                        └───────────────┬────────────────────────────────┘
                                        ▼
                        ┌────────────────────────────────────────────────┐
                        │ 4. CLASSIFY → RUNBOOK                          │
                        │ Match fingerprint against a curated            │
                        │ alert-type → runbook registry (Confluence-     │
                        │ backed, retrieved + cached). Unknown type →    │
                        │ HUMAN CHECKPOINT (no matching runbook).        │
                        └───────────────┬────────────────────────────────┘
                                        ▼
                        ┌────────────────────────────────────────────────┐
                        │ 5. DIAGNOSE (read-only, always allowed)        │
                        │ Run the runbook's evidence-gathering steps:    │
                        │ Thanos/PromQL, Elasticsearch/Kibana, CloudWatch│
                        │ /Azure Monitor, Edge UI/GraphQL task queries,  │
                        │ engine_task_stats-style scripts. Assemble      │
                        │ evidence bundle (metrics, error samples, scope)│
                        └───────────────┬────────────────────────────────┘
                                        ▼
                     ┌──────────────────┴──────────────────┐
                     ▼                                     ▼
   ┌───────────────────────────────┐      ┌────────────────────────────────┐
   │ 6a. AUTO-REMEDIATE            │      │ 6b. HUMAN-IN-THE-LOOP CHECKPOINT│
   │ Only actions on the approved  │      │ Triggered when ANY of:          │
   │ safe-action list, executed    │      │ • no/ambiguous runbook match    │
   │ via the action layer with     │      │ • destructive/irreversible step │
   │ least-privilege creds; verify │      │ • prod rollback / data change   │
   │ recovery metric afterwards.   │      │ • judgment step in runbook      │
   │ One retry max; failure →      │      │   ("consult team", "ask Anton") │
   │ escalate (6b).                │      │ • severity ≥ critical/customer- │
   │                               │      │   facing • repeat within 24 h   │
   │                               │      │ • remediation attempt failed    │
   │                               │      │ Posts evidence + proposed action│
   │                               │      │ + approve/deny buttons; pages   │
   │                               │      │ owning team per runbook contacts│
   └───────────────┬───────────────┘      └────────────────┬───────────────┘
                   ▼                                        ▼
                        ┌────────────────────────────────────────────────┐
                        │ 7. COMMUNICATE                                 │
                        │ Post/update ONE structured summary in          │
                        │ #comms-noc, matching today's convention        │
                        │ (Alert: + incident link, thread for evidence)  │
                        │ plus standardized fields: severity, start time,│
                        │ affected service/env/orgs, owner, actions      │
                        │ taken, status. Recurrences append to thread.   │
                        └───────────────┬────────────────────────────────┘
                                        ▼
                        ┌────────────────────────────────────────────────┐
                        │ 8. RESOLVE & CLOSE                             │
                        │ Watch for RESOLVED (VictorOps/metric clear),   │
                        │ confirm recovery metric held for N minutes,    │
                        │ post closing summary (duration, root cause if  │
                        │ known, actions, follow-ups), write audit log,  │
                        │ file runbook-gap note when step 4 failed.      │
                        └────────────────────────────────────────────────┘
```

**Proposed #comms-noc summary template** (formalizing current practice):

> **Alert:** *Incident #N — <title>* (link) · **Severity:** <warn/critical> · **Started:** <UTC> · **Env/Service:** <env, engine/service> · **Scope:** <orgs/envs affected> · **Owner:** <agent | @team> · **Actions:** <taken/proposed> · **Status:** Investigating / Awaiting approval / Mitigated / Resolved

---

## 3. Per-Alert-Type Automation Matrix

Classification: **FA** = fully automatable now · **HC** = automatable with human checkpoint · **NA** = not automatable yet (diagnose-and-escalate only).

| # | Alert type (source) | Class | Reasoning |
|---|---|---|---|
| 1 | Engine failure rate >15% / 100% (Alertmanager→VictorOps) | **HC** | Diagnosis is fully mechanizable (Edge UI/API task queries; helper script exists; runbook current, Jul 2026). But the runbook's core action is a *judgment* decision tree — route to Engines vs Processing vs Data team — and fixes are code changes. Agent auto-acks, gathers evidence (error type, engine ID, org scope), proposes routing; human confirms escalation target initially. |
| 2 | KubePodCrashLooping / KubePodNotReady (Alertmanager) | **HC** (prod) / **FA-suppress** (chronic stg) | Evidence gathering (pod events, restart counts, recent deploys) automatable; restart/rollback of prod workloads needs approval. Chronic staging fingerprints (aiw-stg198) go to a suppression list with a weekly digest instead of per-fire handling. |
| 3 | KubePersistentVolumeFillingUp / DiskSpaceUtilization (K8s & node-exporter) | **HC** | Trend analysis automatable; expansion/cleanup actions are state-changing. The documented Azure v3f docker data-root fix ends in "delete the VM" — irreversible → checkpoint. |
| 4 | PandoLogic SQL41 `M:` drive shadow-copy cleanup | **HC → FA candidate** | Runbook is precise, scoped, and bounded ("delete copies older than 3 days, M: drive only"). Scriptable via PowerShell. Deletion is technically irreversible, so start HC; promote to FA after shadow-mode evidence. |
| 5 | PandoLogic VM/Host CPU & memory ActiveYellowAlarms | **HC** | Runbook is "check Grafana, watch trend, escalate SRE after 15 min" — the agent can do the checking and the timed escalation automatically; any resize/cleanup involves a named human ("ask Anton") → checkpoint by design. |
| 6 | PandoLogic IMatchC2P down | **HC** | Requires temporal judgment: planned Wednesday restart window; act only if down >30 min. Agent can encode the wait rule and evidence; restart action gated. |
| 7 | Runscope failures — SOLR reindex, Public Stream, CNBC (Runscope→VictorOps) | **HC** | Checks (NSQ depth, Kafka consumer lag, S3 file listing, CloudWatch Insights) are all read-only and automatable; frequent flap/auto-resolve means agent can close trivially. Vendor escalation (VidVita email) is customer/vendor-facing → human approves send. |
| 8 | GraphQL fatal errors / high non-200 / API 5xx | **HC** | Runbook itself says wait 5–10 min for self-heal (automatable watch), then cycle fastcore via existing Jenkins job (good HC candidate), then rollback — which the runbook explicitly gates on "consult the aiWARE team" → hard checkpoint. |
| 9 | API throttling (429s) | **NA → HC later** | Root-cause requires identifying the offending token in the SSO database and a business decision on raising `rate_limit.config_token`. Agent can automate identification (Kibana query → token → org); the limit change affects a customer → human decision. |
| 10 | NSQ / engine backlog (Alertmanager + NOC-AUTOMATION heuristics) | **HC** | Detection heuristics already codified (6 NSQ rules, 2 backlog rules). Diagnosis fully automatable; remediation per NSQ runbook varies (restart consumers vs wait) → checkpoint until action patterns are proven. |
| 11 | Zabbix / email-sourced host memory (REALMATCH) | **HC** | Trend check automatable; memory increase follows a change-managed runbook → approval. |
| 12 | Imperva SSL/domain revalidation emails | **NA** | External vendor console + CA revalidation with a deadline; customer-impacting and account-privileged. Agent's job: parse deadline, create tracking ticket, remind — not act. |
| 13 | Jenkins Standard Apps smoke test UNSTABLE | **FA** (triage & report) | Pure signal-processing: job already retries 4×; agent summarizes persistent failures per env, links test report, opens/updates a tracking issue when the same env fails N consecutive runs. No infra action taken. |
| 14 | ThanosReceiveForwardFailures / Thanos self-monitoring | **HC** | Monitoring-stack health; restart of monitoring components is low-blast-radius but still state-changing; automate diagnosis, gate restarts initially. |
| 15 | Deployments/* incidents & ITSM change windows | **NA (correlation only)** | Rollback of a deploy is a human judgment by policy (deployers explicitly ask the channel to watch and roll back). Agent's high-value role: correlate alerts with active ITSM windows and *suppress/annotate* rather than act. |
| 16 | Threat alerts (realmatch sites) | **NA** | Security-domain; route to SOC (per NOC↔SOC coordination), never auto-remediate. |
| 17 | Solr shard down / Kafka broker down / Rendition Manager stall (legacy runbooks) | **HC** | Documented restart/terminate procedures exist but are SSH-based on legacy hosts with 2020-era docs; verify currency before wiring; keep behind approval. |
| 18 | Unknown / no matching runbook | **NA by rule** | Hard guardrail: classify, gather generic evidence, page on-call, and file a runbook-gap ticket. |

---

## 4. Architecture Components (conceptual)

1. **Slack Event Listener** — subscribes to `#alerts-devops` (message + thread events). Parses the five known bot formats into a normalized alert schema (source, name, env, entity, severity labels, links, VictorOps incident #). Also listens in `#comms-noc` for approval responses and human overrides ("agent stand down").
2. **Ingestion Normalizer & De-duplicator** — fingerprinting, flap detection, chronic-alert suppression list, and correlation of the multi-path duplicates identified in §1.5. Maintains one incident object per real-world condition.
3. **Acknowledgment Adapter** — VictorOps API client to ack/resolve incidents under an agent service identity (distinct from any human user), plus Slack threading for visibility.
4. **Runbook Registry & Retrieval** — a curated mapping table (alert fingerprint → runbook page/section → parsed step list), backed by live Confluence retrieval so runbook edits propagate. Each runbook step is tagged at curation time as `read-only`, `safe-action`, or `human-judgment`. **The registry is the contract:** the agent may only execute steps that exist, tagged, in the registry — it never improvises from free-text runbook prose at runtime.
5. **Diagnostic Toolbelt (read-only)** — Thanos/PromQL, Elasticsearch, CloudWatch/Azure Monitor, Grafana dashboard API, Edge UI/GraphQL task queries, Jenkins build API, S3 listing. This largely already exists in the NOC-AUTOMATION scripts and its master runbook; reuse those queries verbatim.
   - **Visual Evidence Capturer (required per v1.1 scope):** the agent must attach the same visual evidence a human posts today. Three mechanisms, in preference order: (a) **Grafana server-side render API** (`/render/d-solo/...`) for failure-rate, backlog, and utilization panels — PNG output, no browser needed; (b) **headless-browser capture** (e.g., Playwright with a read-only service account) of Edge UI/Controller views — Processing Tasks summary, failed-task list filtered by org, task/job detail — matching the exact screenshots NOC posts now; (c) **agent-rendered charts** from the same GraphQL/PromQL data as a fallback where UI authentication is not yet provisioned. Log-file evidence (task error JSON, downloaded job logs) is attached as code blocks/files exactly as in current practice. Every image is captioned with source, time range, and query so evidence is reproducible.
6. **Action Execution Layer** — small catalog of parameterized, pre-approved actions (e.g., "trigger Jenkins CycleProdFastcore", "delete SQL41 M: shadow copies >3 days", "re-run Runscope test"). Each action: least-privilege dedicated credential (replacing the personal `hpatel` assumed roles currently baked into automation), input validation, dry-run mode, post-action verification metric, and automatic rollback note. No general shell/SSH capability in v1.
7. **Escalation & Approval Engine** — encodes the checkpoint triggers (§2, box 6b); posts approve/deny interactive prompts to `#comms-noc` and pages the correct VictorOps routing key / team @mention from the runbook's Contacts section; enforces approval timeouts (unanswered → page next tier, never "assume yes").
8. **Incident Communicator** — renders the standardized summary (template in §2), posts and updates a single thread per incident in `#comms-noc`, appends recurrences, posts the closing summary.
9. **Audit & Learning Store** — append-only log of every alert seen, classification, evidence gathered, action taken/proposed, approver, and outcome; feeds weekly reports (noise stats, runbook-gap tickets, promotion candidates from HC→FA).
10. **Policy/Guardrail Layer** — a declarative rules file (checked into version control, human-reviewed) that the execution layer consults before any non-read-only call; the LLM/agent reasoning cannot override it.

---

## 5. Guardrails & Escalation Rules

**The agent must never act autonomously when:**

- No runbook fingerprint match, or classification confidence is low → evidence + page, file runbook-gap ticket.
- The action is destructive or irreversible: deleting data/volumes/VMs, terminating prod instances, DB `UPDATE`s (e.g., the requeue-jobs SQL, `rate_limit` changes), certificate operations.
- The action is a production rollback or deploy-adjacent (the Defcon runbook itself mandates consulting aiWARE first; deployers explicitly reserve rollback judgment).
- The action is customer- or vendor-facing (VidVita emails, Imperva console, org-level rate limits) — drafts only, humans send.
- Security/threat-category alerts — route to SOC, no remediation.
- A prior automated attempt on the same incident already failed once, or the same fingerprint fired ≥3 times in 24 h (pattern suggests the "fix" isn't fixing).
- An ITSM change window is active for the affected environment — annotate and hold rather than remediate against an in-flight deploy.
- Any credential, permission, or endpoint outside the pre-approved action catalog would be required.

**Additional operating rules:** every action (including read-only) is audit-logged with incident ID; the agent identifies itself in all Slack posts; a single "kill switch" (Slack command + env flag) reverts the channel to human-only handling; approvals expire and escalate rather than default-allow; the agent never edits runbooks autonomously (it files suggestions).

---

## 6. Risks, Open Questions, and Recommended Rollout

### Risks
- **Alert noise overwhelms value:** without the suppression/dedup layer (§1.5 items 1–2), the agent amplifies noise instead of reducing it. Mitigate: dedup ships in phase 0.
- **Stale runbooks executed literally:** 2020-era pages contain dead links and superseded procedures. Mitigate: registry curation step with per-runbook freshness review; agent only runs curated steps.
- **Credential blast radius:** current automation uses personal SSO roles and pages embed tokens. Mitigate: dedicated service principals per action, secrets rotation before launch (this is worth doing regardless of the agent).
- **Prompt-injection / untrusted input:** alert payloads and Confluence text are untrusted input to an LLM-driven agent. Mitigate: policy layer outside the model; action catalog with typed parameters; no free-form command execution.
- **Ack-SLA regression:** current human ack is ~15 s; an agent that acks instantly but investigates poorly could mask real incidents. Mitigate: shadow-mode comparison against human handling before taking over ack.

### Open questions (need owner answers)
1. Does VictorOps remain the paging system of record (API access, service account, routing keys per team)?
2. Who owns the alert→runbook registry curation, and can runbook owners commit to tagging steps (read-only / safe / judgment)?
3. Is the `#alerts-devops-testing` channel (created May 2026) intended as this project's sandbox, and is there an existing initiative there to align with?
4. Which teams sign off on the initial safe-action catalog (proposed v1: Jenkins CycleProdFastcore trigger, Runscope re-run, SQL41 shadow-copy cleanup, chronic-alert suppression)?
5. What is the policy for staging environments — may the agent act more autonomously there (e.g., restart aiw-stg198 workloads) to burn down chronic noise?
6. Are there compliance constraints (us-gov/SLED environments) that exclude certain environments from any automated action?

### Recommended rollout plan
- **Phase 0 — Hygiene (prereq):** rotate exposed tokens; create agent service identities; fix PandoLogic double-posting; build fingerprint/dedup; agree the #comms-noc template with the NOC team.
- **Phase 1 — Shadow mode (2–4 weeks):** agent runs full pipeline in `#alerts-devops-testing`: acks nothing, but drafts classification, evidence bundle, proposed action, and summary for every incident. Weekly scoring vs. what humans actually did (classification accuracy, evidence usefulness, proposed-action agreement).
- **Phase 2 — Assist mode:** agent posts evidence + proposed action into real incident threads; humans ack and act. Auto-summaries to `#comms-noc` go live. Promotion criteria defined per alert type (e.g., ≥95% classification agreement over ≥20 incidents).
- **Phase 3 — Supervised autonomy:** agent acks VictorOps incidents and executes the small FA/graduated-HC catalog (smoke-test triage, chronic-noise digests, timed escalations, then SQL41 cleanup) with approve-buttons for everything else.
- **Phase 4 — Expand:** per-type promotion HC→FA driven by audit data; extend action catalog only through the change-managed registry; quarterly runbook-freshness review feeding both agent and humans.

---

## Appendix A — Evidence pointers
- Ack latency samples: VictorOps incidents #119666, #119668, #119672, #119673, #119674 (created→ACKED deltas 11–17 s).
- Full manual triage lifecycle example: incident #119665 thread in `#comms-noc` (evidence → org scoping → escalation to app owners → PR revert identified).
- Chronic noise example: `aiw-stg198 KubePodCrashLooping`, continuous FIRING:43–55, observed 21 Jul and 21 Aug 2026.
- Existing automation baseline: *NOC – Infrastructure Monitoring Status – Automation* (Confluence DEV, page 5092737035), NOC-AUTOMATION bot posts every 2 h in `#comms-noc`.
- Runbook inventory: 17 pages under *Runbook: On-Call* (page 1176701630); *Runbook: PandoLogic Alerts* under *PandoLogic Runbooks*; explicit gaps noted in *Prod Issues / Defcons / Alerts* (page 840073685, last edited 2020).

---

## Appendix B — Reference behavior & SOP intake

### B.1 Reference behavior the agent must replicate (from incident #119679)

The manual handling of *Incident #119679 — aiw-wpsc01: Engine failure rate 100% (Docling Chunk Engine)* is the acceptance benchmark for the Engine-failure alert type. The agent's thread in `#comms-noc` must contain the same elements, in the same order:

1. Top-level post: `Alert:` + blockquoted VictorOps incident link/title (posted within minutes of ack).
2. Engine identification: engine name + engine ID as inline code.
3. Quantified impact statement with **screenshot** of the Edge UI task summary ("Over the past 8 hours, all 10 tasks processed by the Docling Chunk Engine have failed — 100% failure rate, 0 completed").
4. Scope statement with **screenshot** of the failed-task list: affected organization (name + org ID) and shared error type (`internal_error`).
5. Raw error evidence: the actual error payload (code, message, source file/line) as a code block, plus job-detail **screenshots** and downloadable log files.
6. Plain-language root-cause narrative ("the PDF downloaded successfully and was read by Docling; the failure occurred when splitting it into smaller text chunks — no valid chunks found → internal error").
7. Escalation @mention to the owning team when the cause is in application code.

### B.2 SOP intake template (one per VictorOps alert type)

For each alert type, please supply the following; the agent will execute *only* what is written here:

| Field | What to provide |
|---|---|
| **Fingerprint** | Exact incident-title pattern(s) that identify this alert type (e.g., `Engine failure rate above 15%`, `Engine failure rate 100%`) |
| **Severity & ack rule** | Default severity; ack immediately or conditions first |
| **Evidence steps** | Ordered list; for each step: tool + query/click-path, screenshot required? (Y/N + which view/panel), what text to post with it |
| **Decision points** | Each judgment fork, the criteria for each branch, and whether the agent may decide or must ask |
| **Safe actions** | Actions the agent may execute autonomously, with exact parameters, preconditions, and post-action verification |
| **Escalation** | Who to @mention / which VictorOps routing key, and the trigger condition |
| **Resolution criteria** | What proves recovery (metric + duration), and what the closing summary must state |
| **Known false positives** | Patterns to recognize and how to disposition them |

### B.3 VictorOps alert types needing SOPs (from observed incidents, ordered by frequency)

1. **Engine failure rate above 15% / 100%** (most frequent — ≥8 incidents in 48 h; runbook exists but decision tree needs codified criteria) — *use #119679 as the golden example*
2. **KubePodsNotReady** (#119660, #119668, #119580)
3. **Runscope: SOLR Asset Reindex Test – PROD** (#119670, #119612)
4. **Wyvern – SQS Message Age DMH** (#119641, #119497)
5. **Deployments/**\* — discovery-app, attribution-app (#119629/630/639/640) — likely "correlate & annotate only," please confirm
6. **Zabbix: High memory utilization (REALMATCH hosts)** (#119672)
7. **Domain revalidation required (Imperva email)** (#119678)
8. **Rekognition-ThrottledCount-High** (#119659)
9. **Azure Prod GOV-2 API Success Rate** (#119517)
10. **AlbUnhealthyHostCritical (us-1 prod)** (#119504)
11. **AWS US illuminate Job** (#119520)
12. **KubePersistentVolumeFillingUp** (#119563)
13. **Threat Alert (realmatch sites)** (#119623) — presumed SOC-route-only, please confirm
