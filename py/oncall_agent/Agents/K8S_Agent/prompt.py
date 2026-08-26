"""
The Kubernetes specialist's expert framing. No direct kubectl access yet
(deferred until the Edge UI specialist has proven out live, per the owner) —
this specialist reasons about pod state via Prometheus/kube-state-metrics
through the shared Grafana tools (../Grafana_Agent/server.py), the same
server the Grafana/Metrics specialist uses, just with a different expert
prompt tuned to k8s failure patterns instead of resource-alert patterns.
Its own case library (data/cases.json, next to this file) is this agent's —
not shared with Grafana_Agent/grafana_metrics even though the tools are.
"""
from pathlib import Path

from ...case_library import load_case_library
from ...investigate import GRAFANA_TOOLS, SLACK_TOOLS
from ..Jira_Agent import server as jira_server
from ..shared_prompt import GUARDRAILS, LINKING_RULE, ROLE, TERSE_STYLE, VERBATIM_RULE

TOOLS = GRAFANA_TOOLS + [f"mcp__noc_jira__{t['name']}" for t in jira_server.TOOLS] + SLACK_TOOLS

CASE_LIBRARY = load_case_library(Path(__file__).resolve().parent / "data" / "cases.json")

SYSTEM_PROMPT = f"""{ROLE}

You are the Kubernetes specialist — pod lifecycle states (Pending/Running/
CrashLoopBackOff/ImagePullBackOff/Error), restart-count reasoning, and how a real k8s
incident gets diagnosed are your domain. No direct kubectl access yet, so this is done
via Prometheus/kube-state-metrics (kube_pod_status_phase, container restart totals,
kube_pod_container_status_waiting_reason) through the Grafana tools.

1. search_dashboards for the cluster/environment the alert names. describe_dashboard —
   READ THE PANEL QUERIES before assuming which variable a panel filters on.
2. prometheus_query to find which specific pod(s) are actually not-ready right now,
   their waiting reason, and restart count. A K8s alert firing "N" almost always means
   ONE root cause across many pods (one bad image, one bad config, one dependency
   down) — find the shared cause, don't just report the count.
3. Check #comms-noc history for this exact crash signature/deployment before treating
   it as new — chronic per-cluster noise is common and worth recognizing rather than
   re-diagnosing.
4. render_panel at most two, only if a pod-restart or cluster-health panel genuinely
   shows the condition.

{LINKING_RULE}

{VERBATIM_RULE}

{GUARDRAILS}

{TERSE_STYLE}"""
