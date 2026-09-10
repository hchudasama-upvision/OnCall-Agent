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
   human has. Each specialist's own `Agents/<Name>/data/cases.json` (split
   from a single `data/cases.json` on 2026-08-26, see below) says WHAT to do
   for an alert type; `panel_map.json` is now only a hint for dashboards
   already pinned.

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
  (`src/`), rewritten in Python under `py/`. The TS tree was superseded and
  removed 2026-08-26 (briefly archived under `legacy/` first, then deleted
  outright — it's in git history if anyone ever needs it back).
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
- **Domain-specialist agents, one folder each (2026-08-24/26).** A single
  generalist juggling Slack + Grafana + Edge UI tools for every alert type
  produced noticeably less sharp results than a human specialist would —
  the owner's read, confirmed once specialists shipped (autonomous engine
  discovery, correct dynamic windows, a Runscope staleness catch, none of
  which the generalist did). `Agents/MASTER_Agent/router.py` is a
  deterministic (not LLM) master router — alert type is almost always
  unambiguous from its own name, so spending a model call to classify it
  would be pure cost. Each `Agents/<Name>/` folder owns everything specific
  to that domain: its MCP server where it has one, its system prompt, and —
  since 2026-08-26 — its own `data/cases.json`, split from the single
  original file (all 12 cases mapped cleanly to exactly one specialist; the
  Grafana agent's graph-alert entries added 2026-08-26 took it to 14, one of
  which — PVC filling up — is a deliberate copy of the Kubernetes agent's,
  see below).
  `case_library.py` stayed generic infrastructure (loads/matches ANY case
  file); `handler.py`'s merged `CASES` exists ONLY for fingerprinting before
  a specialist is even chosen and for the generalist fallback — a
  specialist's own investigation always uses its own agent's case library,
  never the merged one, so a Kubernetes case can never end up grounding an
  Edge UI decision by fingerprint coincidence.
- **The Grafana agent's graph-alert knowledge (2026-08-26).** The four alert
  types whose answer is a Grafana panel — PVC/persistent volume filling up,
  Windows disk space (warning + critical), Windows memory high, VMware VM
  red/yellow alarms — now carry a `dashboard` block in the Grafana agent's
  own `grafana_metrics/data/cases.json`: dashboard uid, panel ids WITH their
  real titles, the variables, and the specific reason each one renders empty.
  That knowledge already existed as static config in `config/panel_map.json`;
  it was moved into the case library rather than expanded there, because a
  case is a head start the model verifies live (non-negotiable #1) while a
  panel_map entry is a decision code makes for it. panel_map is unchanged and
  still the deterministic hint. Every uid/panel id/variable/PromQL in those
  blocks was re-read live against thanos-grafana on 2026-08-26, not copied on
  faith — which is how the `$cluster` note in the PVC entry was found (those
  panels filter `cluster="$cluster"` and the Thanos series carry no cluster
  label at all, so leaving it unset is correct and setting it renders
  nothing).
  PVC filling up is the one alert type deliberately present in TWO agents'
  case files (owner's call): routing still sends it to the Kubernetes
  specialist, and each copy carries a `_shared_with` field naming the other.
  Duplicated knowledge drifts — if one is edited, edit both.
- **Agent-wise write-back memory (2026-08-26), `agent_memory.py`.** The
  case library is curated once by a human and goes stale; nothing fed the
  agent's own past real conclusions back to it. Each specialist now writes
  a compact, already-validated record (root cause, owning team, should_post)
  after every investigation, keyed by (specialist, alert fingerprint), under
  `.state/memory/` — runtime state, not source-controlled, same tier as
  `.state/audit/`. The next investigation of that exact alert type gets its
  last few real outcomes back in a clearly separate, clearly-labeled prompt
  section (never merged into the case-library text) with the same
  verify-don't-trust framing as a case-library entry: a memory can be stale
  or simply wrong, and the model is told to check current evidence before
  repeating it. Only written after `_validate()` passes, so a failed run is
  never memorized as if it were a finding.
- **Engine backlog moved to the Edge UI agent (2026-08-31, owner).** "Engine
  backlog critical for 30m" is the SAME family as "Engine failure rate above
  15%" — engines, tasks, this agent's tools — not a Grafana metrics alert. Its
  case moved from `grafana_metrics/data/cases.json` to
  `Edgeui_Agent/data/cases.json`, and the backlog keywords moved to an `edge_ui`
  route (its first keyword route ever: engine-failure alerts are matched by
  `is_engine_failure_rate()`'s regex instead).
  The graphs are in Edge UI, on `/processing/jobs/`, in the card titled
  "Backlog". Two new tools: `fetch_engine_backlog` and
  `capture_backlog_screenshot`.
  DO NOT SCRAPE THE CARD. It is an ApexCharts SVG whose only readable text is
  the engine-name legend — a DOM scrape returns a list of engine names and no
  numbers at all (that is what the first probe returned). The numbers come from
  the endpoint the card itself draws, found by watching the network on a real
  environment: `GET /edge/v1/proc/jobs/backlog_count_by_engine?startTime=&endTime=`
  -> `{counts: [{engineID, engineName, priority, values: [[epoch_ms, count]]}]}`.
  `edge_api.fetch_backlog_by_engine` parses that into now/peak/trend per engine,
  where `trend` compares the series' own two HALVES rather than first-vs-last —
  a single spike at the start otherwise reads as "draining" forever.
  The card is located by its `.ant-card-head-title` TEXT, never by its class:
  `TimeSeriesChart_cardChart__9AQva` is a build-hashed CSS-module name that
  changes on every Edge UI deploy. Capture waits for drawn SVG content and for
  the ant-spin spinner to clear, then clips the card element — the same
  discipline as the Grafana panel capture, for the same reason.
  `_BACKLOG_FORMAT` in prompt.py gives this alert type its own two-reply field
  block; the seven-post structure stays engine-failure-only, and the prompt now
  opens by saying which family gets which format.
  Verified live 2026-08-31 on aiw-prod1001: `1439` queued across `31` of `36`
  engines, and the run went further than asked — it checked whether the queue
  was failing rather than waiting, found `1074` of `8944` Podcast Adapter tasks
  dying with `connection.ETGN056` (an RSS feed returning HTTP `410`), and said
  the queue would not drain on its own.
- **VM alarms got processes, memory and disk — and a Windows login surface
  (2026-08-31, owner).** A `VM ActiveRedAlarms` thread now renders THREE panels
  for the VM (17 CPU, 18 memory, 48 disk — panel_map already mapped all three)
  plus the top 5 processes by CPU and by memory, with the text cut to a compact
  field block. Three images is a scoped exception to `EVIDENCE_ECONOMY`, and it
  passes that rule's test: CPU, memory and disk answer different questions.
  `Agents/Windows_Agent/` has two sources for "which process", in this order:
  (1) PROMETHEUS, NO CREDENTIALS. 93 Windows hosts run windows_exporter with the
  process collector, so `windows_process_cpu_time_total` and
  `windows_process_working_set_bytes` are already in Thanos, 218-442 processes
  per host. Verified on SVC182: top CPU `consul` `1.12%` of one core, top memory
  `w3wp` `2701MB`. `Idle` is EXCLUDED — it is per-core idle time and measured
  593.68% on that host, so it wins every unfiltered top-5. Units differ by
  source and the tool labels which: a 5m rate as percent of ONE core from
  Prometheus (so >100% is legitimate on a multi-core host), cumulative CPU
  SECONDS from WinRM.
  (2) WINRM, only for hosts with no exporter. `winrm_client.py` is
  QUERY-SHAPED, NOT COMMAND-SHAPED: six named read-only PowerShell strings live
  as literals in that file and the caller picks one by NAME. There is no
  passthrough parameter and no interpolation, so `Stop-Process`,
  `Restart-Computer` and every `Set-*`/`Remove-*` are unreachable rather than
  discouraged. That matters more here than anywhere else in the repo, because
  WinRM can otherwise run anything.
  Credentials: `WINDOWS_USERNAME`/`WINDOWS_PASSWORD` in `.env` (gitignored),
  read inside the client, never in a prompt, never on a command line (which
  would expose them in the TARGET's process list), and scrubbed out of any error
  text. Same inherited-credential problem as `EDGE_USERNAME` and worse: this one
  can log into every VM in the domain while the agent needs four counters. A
  read-only service account (Performance Monitor Users / remote WMI) is the
  right end state.
  STG-Backend5, the VM in the owner's own alert, has NO exporter and its WinRM
  AUTHENTICATES but refuses to create a shell — `0x80070002` for every command,
  including `hostname` via cmd, so it is the remote-shell plugin on that VM, not
  the credential. The tool therefore reports the breakdown as UNAVAILABLE and
  says not to infer a process from the CPU graph. Installing windows_exporter
  there is the better fix than enabling the shell: it removes the credential
  from the path entirely.
  A bug worth remembering: the exporter lookup strips the DNS suffix (metrics
  key on the bare hostname) and the first version passed that stripped name to
  WinRM, which failed on DNS instead of reaching the host — only the FQDN
  resolves. `_resolvable_names()` now keeps the two separate.
  Verified live on the real alert: CPU `96.875%` of the VM limit with a `6h`
  peak of `107.87%`, memory `12.99%`, and one guest volume `100%` full with `0`
  bytes free — which only showed up because the disk panel was added.
- **VMware ESXi host alerts (2026-08-31, owner).**
  `HostCPUUtilizationCritical` / `HostMemoryUtilizationCritical` route to
  `grafana_metrics` and are answered by the "VMware ESXi" dashboard
  (`ead34884-4dca-4c80-bff3-a3aac2c4dc34`), `var-esxhost` = the ESXi host:
  panel 17 (Host CPU usage timeseries) and 12 (the gauge), 18/13 for the memory
  variant, 42 (Host's VM CPU usage) as the root-cause panel when the question is
  which guest is driving it. Verified live, `from=now-15m` as the owner's link
  has it — 16 datapoints at that width, so the window is fine.
  THE ALERT NAMES TWO FQDNs AND ONLY THE FIRST IS THE SUBJECT: label 2 is the
  ESXi host (series label `host_name`), label 3 is the vmware_exporter that
  scrapes vCenter (series label `instance`, identical on EVERY host's series).
  Graphing label 3 would chart the wrong machine. That is why the panel_map
  entry pins `var-esxhost: "{label:2}"` and says why.
  These are also Thanos rules (`node.rules`), both `> 85` `for 10m`, so
  `thanos_alert_status` supplies the threshold and the duration wording, and the
  `thanos_usage` re-check re-renders the ESXi PANEL rather than the Thanos UI
  when `panel_map` has one — `PendingCheck.params` carries the resolved
  dashboard/panel/variables so the re-check does not have to re-derive them from
  an alert it no longer holds.
  A REAL BUG THE MODEL CAUGHT FIRST: the ESXi rules select a whole CLUSTER
  (`cluster_name=~"RealMatch-Cluster01|RealMatch-Cluster03"`), so `max()` over
  the rule expression reports the worst host in the cluster. The investigation
  said so unprompted — "the rule selector is cluster-wide, so its 6h peak of
  105.6% is ny1esx9681, not the host named here" — while the re-check was
  quoting that 105.63 as if it belonged to the alerting host.
  `_scoped_expr()` now injects the alerting resource's label into EVERY metric
  selector in the expression (both halves of a division, or the ratio mixes two
  machines), and `_VARIABLE_TO_LABEL` maps panel variables to series labels for
  only the dashboards actually pinned — a guessed mapping would scope to a
  label that does not exist, return nothing, and read as "recovered". The reply
  now states its scope explicitly, and says when it has none.
- **PromQL-rule alerts, straight from Thanos (2026-08-31, owner).** "High
  concurrent_requests for core-admin-server" and "High nodejs_active_handles
  for …" are answered by one query on https://thanos.ops.veritone.com — a
  DIFFERENT host from `thanos-grafana.ops.veritone.com`, reachable with no auth
  from inside the VPN. `Agents/Grafana_Agent/thanos.py` + two tools on the
  Grafana MCP server (`thanos_alert_status`, `thanos_graph`).
  THE ALERT TITLE IS THE RULE NAME. `/api/v1/rules?type=alert` returns
  `aiw:zpfc02 - High concurrent_requests for core-admin-server` with query
  `concurrent_requests{env="aiw-zpfc02",job="core-admin-server-service"} > 10`
  and `for: 300` — so the threshold, the exact selector and the evaluation
  window are a LOOKUP, not a guess. Nothing else in this repo could see those
  rules. That is also what makes the owner's requested wording legitimate: "high
  for 2h10m" / "low again, came back below 12m ago" is computed by
  `breach_summary()` walking the range series back to the last threshold
  crossing, and phrased by a fixed template in code — never by the model.
  THE INSTANCE IN THE ALERT GOES STALE. The owner's own example,
  `instance="10.244.188.5:9000"`, returned an empty vector: pod IPs churn and
  that pod is gone. `resolve_series()` falls back to the rule's own selector and
  RETURNS A NOTE saying it did, because "no data" for a healthy service is the
  wrong answer and a silent substitution is worse.
  The graph is a screenshot of the real Thanos UI with the query bar in frame
  (so the picture carries its own provenance), clipped above the legend — which
  lists 7 series with ~10 labels each and is three times the height of the plot.
  `oncall_agent.chart` is the fallback, and the caption says which rendered it.
  `chart.py` moved from `Agents/AWS_Agent/` to package level in the same change:
  two agents needed it once CloudWatch would not render in GovCloud and a Thanos
  query had no dashboard to screenshot at all.
  `follow_up.py` gained its fourth kind, `thanos_usage`, posting the number, the
  explanation and a fresh graph at EVERY attempt (+5 and +10) — the owner asked
  for periodic screenshots. It confirms the rule exists in Thanos before arming:
  no rule means no threshold, and a re-check without a threshold cannot say high
  or low.
  Verified live on the real rule: `Concurrent request: 1 — threshold 10`, 7 pods
  reporting, 73 datapoints over 6h, plus two re-check posts each with their own
  Thanos screenshot.
- **GovCloud, and a CloudWatch renderer that does not work there
  (2026-08-31, owner).** `Rekognition-ThrottledCount-High-wpsc01` is the one
  gov-AWS alert in the same shape as the commercial ones, so it went to the AWS
  agent rather than getting its own. Three generic tools joined the RDS ones:
  `cloudwatch_alarm` (what the alarm actually watches, its state, its recent
  state changes), `cloudwatch_metric_graph` and `cloudwatch_metric_values` —
  generic in namespace/metric/dimensions, so the next CloudWatch-alarm alert
  needs no new code. `find_alarm()` searches every allowed account/region and
  reports which one it found, because an alarm name is not unique across
  accounts and this repo now spans two partitions.
  CREDENTIALS ARE NOT SSO HERE. `us-1-gov` assumes
  `VeritoneGovInfraOpsAssumeRole` in GovCloud account `113765098011` from STATIC
  keys in `~/.aws/credentials [vtgovuser]`. The expired-credential message was
  therefore reworded: telling someone to run `aws sso login` for a key-based
  profile sends them down a dead end.
  THE BIG ONE: `cloudwatch get-metric-widget-image` DOES NOT WORK in that
  GovCloud account. It answers `Throttling: Rate exceeded` every single time,
  including after three retries with backoff, while the byte-identical call
  against the commercial account returns a PNG immediately. The metric DATA is
  fine there. So `Agents/AWS_Agent/chart.py` draws the series locally — inline
  SVG screenshotted with the Playwright already in the dependency list, no
  plotting library, nothing fetched at render time — and the caption SAYS it was
  drawn from CloudWatch datapoints rather than being a console screenshot. That
  distinction is not cosmetic: a reader is entitled to know which renderer they
  are looking at.
  `aws_client.run()` also gained a retry for genuinely transient AWS errors
  (Throttling/RequestLimitExceeded/ServiceUnavailable) with backoff, and
  deliberately does NOT retry auth or not-found failures.
  `follow_up.py` gained a third kind, `cloudwatch_usage`, which posts at EVERY
  attempt (+5 and +10) rather than once — the owner asked for multiple captures
  across that window. Its alarm resolver CONFIRMS the alarm name against
  CloudWatch before arming, so a re-check cannot go hunting a name that does not
  exist in a thread nobody is watching. When the chosen window is silent it
  widens the GRAPH once (24h -> 168h) and says so in the reply, because this
  metric goes quiet between bursts: on a real run the 3h graph found no
  datapoints and correctly refused to draw an empty chart, leaving the re-check
  with no picture at all.
  Verified live: the alarm went `OK -> ALARM` on 2026-08-27T18:12Z with
  `ThrottledCount` running 39,172 -> 199,075 -> 265,093 per 5-minute bucket
  against a threshold of `100`, peaking at `787.8K`; the investigation reported
  `9,319,423` throttles against `10,201,223` calls over 96h and captioned its
  locally-drawn graph correctly.
- **A fifth specialist: `Agents/AWS_Agent/` (2026-08-31, owner).** RDS alerts
  (`[FIRING:1] us-1 : prod - RDS_CPUUtilizationAvgCriticalCore`) get their own
  agent because the evidence is in AWS, not Grafana: CloudWatch for graphs and
  numbers, Performance Insights for top SQL, RDS events for a failover or
  parameter change. Six read-only tools — `list_rds_instances`,
  `rds_instance_summary`, `rds_metrics`, `rds_metric_graph`,
  `rds_top_queries`, `rds_events`.
  `cloudwatch get-metric-widget-image` renders the graph PNG SERVER-SIDE, so
  this path needs no Playwright, no login session and no settle-time polling —
  the entire class of "screenshotted the word Loading" bugs the Grafana path had
  to solve does not exist here. Two graphs per thread (owner's direction): CPU
  and connections, one metric each, because they have different units.
  Credentials: the operator's own `aws sso login` session, the same deliberate
  exception as `gh`/`kubectl`. Guardrails in code: an allow-list of
  (service, subcommand) READ pairs — `reboot-db-instance`, every `modify-*` and
  every `delete-*` are unreachable, not merely discouraged — plus validated
  identifiers before argv, and `AWS_PROFILES` as an account allow-list so an
  alert cannot steer the agent into an account nobody named.
  `find_db_instance()` SEARCHES for the instance across the allowed
  profiles/regions rather than assuming, and reports which account it found,
  because a same-named database in another account is a real hazard.
  `follow_up.py` gained a `kind`: `rds_usage` re-renders the CPU and connection
  graphs at +5min and posts them unconditionally (the owner asked for the graph
  again, not for a threshold), where `api_health` still posts only on a verdict.
  Its instance resolver matches WORDS, not substrings, and splits camelCase —
  plain substring matching found "core" inside "CriticalCore" and "rds" inside
  "RDS_CPUUtilization", scored every instance, and picked a database the alert
  never named; generic words (`rds`, `db`, `cluster`) are dropped for the same
  reason the api_health panel pairing drops them. An ambiguous alert resolves to
  NOTHING and logs why.
  One bug worth remembering: `aws_client` read `AWS_PROFILES` at import time,
  and `follow_up` imports it during handler import — before `load_dotenv()`. The
  allow-list froze empty and the +5min re-check went looking in the `default`
  profile. Profile/region/timeout are now read from the environment on every
  call; treat any module-level `os.environ.get` in this package as suspect.
- **ALB target health moved to the AWS agent (2026-08-31, owner).**
  `ALBUnhealthyHostCritical` used to route to `grafana_metrics` with a case that
  said "check the target group, wait for warm-up" and no AWS tools at all. The
  evidence is ELB target health, so it moved: routing and the case now belong to
  `Agents/AWS_Agent/`, with two tools — `alb_target_health` and
  `alb_health_graph`.
  THE BALANCER IS NAMED ONLY IN THE SUMMARY, as an ARN tail
  (`app/uk-prod-fastcore-app-http/992c86a8ba9f5fb3`). `find_load_balancer()`
  accepts the tail, the bare name or a full ARN, and searches the allowed
  profiles/regions — `uk-prod` is eu-west-2, the first non-us-east-1 commercial
  region here, so `AWS_REGIONS` had to grow.
  The CloudWatch dimension is that same ARN TAIL, not the load-balancer name,
  and the target group's dimension is the tail of ITS arn
  (`targetgroup/<name>/<id>`). Using the names returns no datapoints, which
  reads as "no data" rather than "wrong dimension". HealthyHostCount and
  UnHealthyHostCount go on ONE graph — same unit, and the ratio is the question,
  which is the one case where two series belong together.
  `TargetHealth.Reason` is the discriminator, and the live run found a fifth
  value the old case never mentioned: `Target.ResponseCodeMismatch` — "Health
  checks failed with these codes: [503]". The instance is up and answering; the
  application behind it is broken. "Wait for warm-up" is the wrong read for that
  one, and replacing the instance would not fix it.
  `follow_up.py` gained `alb_health` AND a per-kind cadence (`minutes_for()`,
  `_KIND_MINUTES`): this family re-checks at +3 and +6 minutes rather than the
  usual +5/+10, because the owner asked for 2-3 minutes and because that is
  exactly the window in which a warming-up target either passes its health
  checks or does not. `FOLLOW_UP_MINUTES` still overrides everything.
  Verified live: `3` healthy / `4` registered, `i-0c94abc2a52f50a11` failing on
  503s, and the graph showed the same instance flapping 4->3 twice in 3h.
  Two bugs this surfaced. `CaseLibrary` indexed one case twice when two of its
  names differed only in letter case ("AlbUnhealthyHostCritical" from the
  fingerprint, "ALBUnhealthyHostCritical" from the alertname) — the case was
  being sent to the model twice. And a re-check reconstructed an evidence
  filename that the render tool had slugified, so a graph that rendered fine was
  reported as missing; `_evidence_paths()` now reads the key back out of the
  tool's own output.
- **EndpointDown, and the change/ITSM check (2026-08-31, owner).** For
  `ops-prom : EndpointDown` the thread names WHICH endpoint is down; the action
  stays a human's, because the first real question is whether an ITSM change or
  recent work explains it, and that context lives with the on-call engineer.
  Two new read-only tools. `endpoint_status` (noc_grafana) reads the alert
  SERIES rather than a dashboard — there is no dashboard for this one, and the
  labels are the evidence: `url`, `status` versus `expected_status_code`,
  `total_time`, and the TLS fields. `search_recent_changes` (noc_slack) reads
  the `CHANGE_CHANNELS` for recent messages mentioning the endpoint's host.
  That one is EMPTY by default on purpose: reading the wrong channel and
  reporting "no change found" is worse than reporting that the check was not
  configured, and it distinguishes those two outcomes in its own output — "the
  absence of an announcement in those channels" is what it says, never "no
  change is happening".
  A real safety finding: MONITORED URLS CARRY LIVE CREDENTIALS. A firing series
  on 2026-08-31 probed a workflow URL with a real `authToken=` in its query
  string, and these threads go to Slack. `server._safe_url` strips
  credential-bearing query VALUES in code (keeping the parameter NAMES, since
  `?authToken=…` vs `?playout` distinguishes two endpoints) — not left to the
  prompt.
  Also note `environment` on this alert is a Route53 hosted-zone id or a
  synthetic test-suite name, not an aiWARE env, and `instance` is always the
  probe host (`localhost:9100`), never the target. One alert usually means
  several endpoints — six were down at once on 2026-08-31, two of them Jenkins.
  This surfaced a live bug in the fabrication guardrail: `BACKTICK_VALUES` tells
  every agent to backtick concrete values, a probed URL IS a value, and
  `_normalize_url` did not strip a trailing backtick — so a complete
  investigation was thrown away as "a URL it never read". It now strips
  `` ` ``/`*`/`_`, and `BACKTICK_VALUES` distinguishes a URL offered as a
  clickable citation (bare) from a URL that is the value being reported
  (backticked).
- **API response-code alerts, and the first timed second look (2026-08-28,
  owner).** `Agents/Grafana_Agent/api_health.py` + `follow_up.py`. Alerts like
  "US-Prod Response Codes - nginx -ai13s aiWARE/prod" are answered by the
  owner-pinned "API Services - Overview" dashboard
  (uid `da6b8dc3-ecd3-4b8c-b7e4e`), which carries every environment's response
  codes and success rate. Two things make this family unlike every other
  Grafana alert here:
  (1) THE ALERT NAME IS THE PANEL TITLE, so `find_panel_pair()` resolves it
  against the dashboard's live titles instead of a static map — a new
  environment on the dashboard needs no code change. Every environment is a
  PAIR (codes timeseries + success-rate stat): 25+26 US-Prod nginx -ai13s,
  16+12 US-Prod haproxy, 15+14 UK, 27+28 Azure prod, 29+30 Azure stage, 9+10
  DMH CrUX, 23+24 DMH Core. Pairing is gated on the `-ai13s` marker and
  penalises extra region tokens, because the naive version paired US-Prod
  haproxy with panel 26 (the -ai13s ingress) — a real bug, and the failure mode
  is one environment's number under another's graph.
  (2) THE NUMBER DECIDES A SECOND POST. These panels are ELASTICSEARCH-backed
  (`es-nginx-prod`), so `prometheus_query` answers nothing; the rate comes from
  replaying the panel's own four Lucene count queries through `/api/ds/query`
  and applying the panel's own math — `1 - 5XX/(2XX+5XX+4XX+NEG)`,
  `percentunit`, 4 decimals. Verified against panel 25's own legend on
  2026-08-28 (2XX 1.08 Mil, 4XX 6.59K, 5XX 30 -> `99.9972%`).
  BOTH panels get screenshotted, first post and re-check alike (owner, same
  day) — the stat is the number a human looks for first, the timeseries says
  which codes moved. That is a deliberate, scoped exception to
  `EVIDENCE_ECONOMY`'s one-image rule, and it passes that rule's own test: the
  two answer different questions. In the re-check they go up in ONE
  files_upload_v2 call so they land together under one comment.
  `follow_up.py` then does what the runbook's "wait 5-10 min for self-heal"
  step says: re-checks at +5 and +10 minutes and posts the recovered (or, at
  the final attempt, still-below) rate plus fresh screenshots into the SAME
  thread. Wholly deterministic — no model call in that file. The threshold
  (99.99%), the timing, the wording and whether anything is posted at all are
  code's, per non-negotiable #1; an intermediate still-below check posts
  nothing, because a "still broken" line every few minutes is noise. Pending
  re-checks are persisted to `.state/followups/` and resumed by
  `listener.run()`, since these alerts cluster around deploys, which is also
  when the agent gets restarted; a record older than
  `FOLLOW_UP_MAX_AGE_MINUTES` is dropped rather than posted late.
  Three gotchas worth keeping: Grafana's Elasticsearch proxy refuses POST
  except on `/_msearch` (so `/api/ds/query` is the only route for an
  aggregation); the ES datasource rejects a query with no aggregation, so the
  panel's own date_histogram is kept and its buckets summed (which is what the
  panel's `reduce` stage does); and a returned frame is
  `[time_column, count_column]`, so summing every numeric value adds epoch
  milliseconds to the count — the first attempt read 6.4e15 requests.
  A `mcp__noc_grafana__api_success_rate` tool exposes the same lookup to the
  Grafana specialist, because without it the first post carried a graph and the
  words "its current value is not visible to me" (real output). Kibana was
  deliberately NOT added: the owner deferred it, and the same log store is
  already reachable through this dashboard's datasource.
- **The Kubernetes specialist got real (read-only) kubectl, 2026-08-27.**
  `Agents/K8S_Agent/server.py` + `kubectl_client.py`. Until then it reasoned
  about pods through kube-state-metrics, which can say "N pods are not ready"
  and never why — no waiting reason, no exit code, no logs, and no way to tell
  one dead pod out of six from six out of six. The owner asked for exactly
  three things in a KubePodsNotReady thread, and `_POD_FORMAT` in prompt.py
  now requires them in order: the pod's STATE (phase/reason/restart
  count/previous exit code/node), its LOGS (`previous=True` for a
  crashlooper — the live container is seconds old, the dead one holds the
  stack trace), and the BLAST RADIUS (`list_replica_peers` walks pod ->
  ReplicaSet -> Deployment and reports desired/ready plus every sibling).
  Five tools: `list_clusters`, `list_unhealthy_pods` (the entry point — a k8s
  alert names a count and a namespace, rarely a pod), `get_pod_state`,
  `get_pod_logs`, `list_replica_peers`. Prometheus is kept for "how long has
  this been true", which kubectl cannot answer.
  Credentials: a deliberate exception to non-negotiable #4, same reasoning as
  `Github_Agent` and the `claude_cli` provider — it shells out to the
  `kubectl` on the box against the kubeconfigs under `~/eks` (generated by
  `~/eks/generate-kube-config.sh`, each with its own `.envrc` naming the AWS
  profile/region, which `kubectl_client._parse_envrc` READS rather than
  sourcing). No token passes through a prompt and nothing here mints one.
  Four guardrails, all in code rather than asked of the model: fixed read
  verbs only (verbs are literals in `kubectl_client.py`, never caller input);
  every name validated against `_NAME_RE` before it reaches argv, so a "pod
  name" of `--as=cluster-admin` is refused rather than becoming a flag (no
  shell anywhere); `KUBE_ENVIRONMENTS` defaults to `stage`, so prod needs an
  explicit opt-in even though every call is a read; and `workload()` refuses
  any kind that is not a workload, so a Secret can never be read through the
  same path. Log output passes `_scrub` first — the ApiSuccessRate case in the
  Grafana agent's own library already records real upstream logs carrying live
  Bearer tokens, and this surface posts log excerpts into Slack.
  Thread format (owner's direction, same day): pod alerts use a LABELLED FIELD
  BLOCK, not prose — `*Env:*` / `*Namespace:*` / `*Pod:*` / `*State:*` /
  `*Restarts:*` / `*Node:*` / `*Age:*`, one field per line, label bold with
  SINGLE asterisks (`**Label:**` renders as literal asterisks in Slack) and
  the value in backticks. Prose could not be read in one glance; a field list
  can. `_POD_FORMAT` explicitly overrides `EVIDENCE_ECONOMY`'s two-reply cap
  for this alert type (three replies plus a bare @mention), the same way
  `Edgeui_Agent`'s fixed format does; every other rule there still binds.
  A real bug this surfaced: the model emitted `owning_team_mention` as
  `&lt;@devops-oncall&gt;`, which Slack renders as literal text — the ping is
  silently lost — and which also tripped the mention-must-appear-in-a-post
  guardrail into throwing the whole investigation away.
  `investigate._unescape_slack_delimiters` now repairs entity-escaped
  mention/link delimiters and the mention comparison normalizes both sides.
  Deliberately narrow: it only rewrites `&lt;…&gt;` sequences that form a
  mention or link, never a URL or an evidence key, so non-negotiable #2's
  abort-loudly property is untouched. A mention in NO post is still a hard
  error.
  Verified live against `aiw-stg198` on 2026-08-27: a real
  KubePodCrashLooping run produced state + previous-container trace + "
  `Deployment/discovery-app-stg198` desired 1, ready 0" in three replies, and
  found the actual chain (an unresolvable `binaries-lb.aws-.veritone.com`
  meant `global_base.json` was never written, so `config.json` was `{}`, so
  `chdir(undefined)` threw) — which no metric could have shown.
- **Observations only, confirmed data only (2026-08-27, owner).** Three rules
  now bind EVERY agent — the four specialists, the tool-using generalist and
  the no-tools fallback — and they live in `Agents/shared_prompt.py`
  (`CONFIRMED_ONLY`, `EVIDENCE_ECONOMY`) so no agent can be given a looser
  standard than its siblings; `investigate.py` imports the same two constants
  for its own two prompts. (1) Every posted sentence comes from the alert or
  from a tool result — no hypotheses, and labelling a guess as a guess does
  not license posting it ("this next bit is a guess" was real output, and a
  human at 3am acts on it). A case-library entry or a memory record is what
  happened BEFORE, so repeating it as current fact is a guess too. (2) Two
  replies is normal, three the ceiling; ONE panel backs the number; no
  standalone caveats/limitations reply — what could not be reached goes in
  `reasoning`, which is recorded and never posted. (3) No recommendations,
  suggestions or next steps at all, and `proposed_action` stays empty: the
  on-call engineer reads the numbers and decides. `EVIDENCE_ECONOMY` carries a
  worked before/after example, which moved the output more than the rules did.
  `Edgeui_Agent` gets (1) and (3) but NOT the generic reply cap — its fixed
  7-post structure is validated (DESIGN.md Appendix B.1), so the same
  discipline is scoped to that structure instead ("these seven ARE the
  thread", no eighth post, two screenshots, no caveat post).
  The lock behind rule 3 is code, not the prompt: `slack_post.
  POST_PROPOSED_ACTION = False` means a model that fills the field anyway
  still cannot post an action request. `compose_action_request`/
  `post_action_request` and the Approve/Deny plumbing are deliberately kept —
  flipping that one flag is the whole rollback.
  A fourth rule joined them the same day: `BACKTICK_VALUES` — wrap every
  concrete value (numbers, hosts, instances, drive letters, metric/label names,
  panel ids, windows, error codes) in single backticks, because that is what
  makes a thread scannable in Slack. It also names the two things that must
  NEVER be backticked: an @mention and a link, since backticks stop Slack
  rendering either one. `Edgeui_Agent` already backticked identifiers in its
  own `_FORMAT`; the shared rule extends that to numbers and windows.
  Measured on the same real alerts before and after: 5 replies -> 2, two
  panels -> one, the inferred "this reads as a misconfigured or stale rule"
  verdict gone, and values now rendering as `118.5%` / `now-6h` /
  `Rubrik_LastBackup` with the mention left bare so it still pings.
- **Read-only GitHub and Jira tools (2026-08-26), `Agents/Github_Agent/` and
  `Agents/Jira_Agent/`.** A real code regression (GitHub) or an
  already-filed ticket (Jira) is frequently the actual answer to "why is
  this firing," and a case-library note written once goes stale the moment
  the real PR merges or the real ticket gets a resolving comment. Not a
  routed specialist — both are an extra toolset layered onto the specialists
  that already exist: GitHub only onto Edgeui_Agent (a real code
  regression is the common case there specifically); Jira onto every
  specialist, because a relevant ticket is not domain-specific the way a
  GitHub code fix is — 2026-08-26 examples: a Rekognition throttling alert,
  a Runscope Track Job failure, and a PandoLogic disk alert each had one.
  GitHub is a deliberate exception to non-negotiable #4: it shells out to
  the `gh` CLI's own already-authenticated OAuth session instead of a
  `.env` token, the same reasoning already applied to the `claude_cli`
  provider above. Jira is NOT an exception — `JIRA_EMAIL`/`JIRA_API_TOKEN`
  are real `.env` credentials, same as every other tool. Both surface a
  found key/URL into the same generic tool-result corpus
  `investigate.py`'s `_allowed_urls` already scans, so no fabrication-check
  changes were needed for either — the model must cite exactly what
  `search_issues`/`get_pr`/etc. actually returned, never a guessed key.

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
