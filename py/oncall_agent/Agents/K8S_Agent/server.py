#!/usr/bin/env python3
"""
Kubernetes tools for the investigating model — read-only kubectl.

The owner's direction (2026-08-27): a KubePodsNotReady / KubePodCrashLooping
thread has to answer three questions, and Prometheus can answer none of them
properly:

    what STATE is the pod in   -> get_pod_state   (waiting reason, exit code,
                                                   restart count, events)
    WHY did it die             -> get_pod_logs    (previous=True for a
                                                   crashlooper — that is the
                                                   container that actually died)
    is this one pod or all     -> list_replica_peers (every sibling of the same
                                                      owner, plus the workload's
                                                      desired/ready counts)

That third one is the difference between "one pod of six is dead, no customer
impact yet" and "the whole deployment is down", and it is the question the
old metrics-only approach could never answer: kube_pod_status_phase gives a
count, not who owns whom.

list_unhealthy_pods exists because a K8s alert usually names a namespace and a
count, not a pod. It is the entry point: find the actual pods, then state/logs/
peers on the one that matters.

Everything here is read-only and enforced in kubectl_client.py, not asked of
the model: fixed read verbs, validated names, stage-only by default, secrets
scrubbed out of log output. Nothing in this file can write to a cluster or post
to Slack.
"""
import json
import sys
from typing import Any, List, Optional

from . import kubectl_client as kube

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "noc-k8s", "version": "0.1.0"}

_READY_PHASES = ("Running", "Succeeded")


def _age(timestamp: str) -> str:
    """Rough age of a k8s timestamp, the way kubectl prints it."""
    from datetime import datetime, timezone
    if not timestamp:
        return "?"
    try:
        started = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return "?"
    seconds = int((datetime.now(timezone.utc) - started).total_seconds())
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d"


def _container_summary(status: dict) -> str:
    """One container's state as a single readable line.

    The interesting half is usually lastState, not state: a CrashLoopBackOff
    pod's CURRENT state is just "waiting, backing off", while lastState carries
    the exit code and when it died.
    """
    parts = [f"{status.get('name')}: ready={status.get('ready')} "
             f"restarts={status.get('restartCount', 0)}"]
    state = status.get("state") or {}
    for kind, detail in state.items():
        bits = [kind]
        for key in ("reason", "exitCode", "signal", "startedAt", "message"):
            if detail.get(key) not in (None, ""):
                bits.append(f"{key}={detail[key]}")
        parts.append("state: " + " ".join(str(b) for b in bits))
    last = (status.get("lastState") or {}).get("terminated")
    if last:
        parts.append("previous run: terminated "
                     + " ".join(f"{k}={last[k]}" for k in
                                ("reason", "exitCode", "startedAt", "finishedAt")
                                if last.get(k) not in (None, "")))
    return "\n    ".join(parts)


def _pod_line(item: dict) -> str:
    meta, status = item.get("metadata", {}), item.get("status", {})
    statuses = status.get("containerStatuses") or []
    ready = sum(1 for c in statuses if c.get("ready"))
    restarts = max((c.get("restartCount", 0) for c in statuses), default=0)
    reason = status.get("reason") or ""
    for c in statuses:
        waiting = (c.get("state") or {}).get("waiting") or {}
        terminated = (c.get("state") or {}).get("terminated") or {}
        reason = waiting.get("reason") or terminated.get("reason") or reason
    node = (item.get("spec", {}) or {}).get("nodeName") or status.get("nominatedNodeName") or "?"
    return (f"{meta.get('namespace')}/{meta.get('name')}  {status.get('phase')}"
            f"{'/' + reason if reason else ''}  ready={ready}/{len(statuses)}"
            f"  restarts={restarts}  age={_age(meta.get('creationTimestamp', ''))}"
            f"  node={node}")


def _is_unhealthy(item: dict) -> bool:
    status = item.get("status", {})
    if status.get("phase") not in _READY_PHASES:
        return True
    if status.get("phase") == "Succeeded":
        return False
    return any(not c.get("ready") for c in (status.get("containerStatuses") or []))


# --------------------------------------------------------------------- tools

def tool_list_clusters() -> str:
    clusters = kube.discover_clusters()
    if not clusters:
        return (f"No kubeconfigs under {kube.KUBECONFIG_DIR} for environment(s) "
                f"{', '.join(kube.ALLOWED_ENVIRONMENTS)} — you cannot see any cluster from "
                f"here. Say that rather than describing pods.")
    return "\n".join(f"{c.name}  [{c.environment}]  aws_profile={c.aws_profile or '?'} "
                     f"region={c.aws_region or '?'}" for c in clusters)


def tool_list_unhealthy_pods(cluster: str, namespace: str = "", limit: int = 25) -> str:
    """Pods that are not Running-and-ready. The entry point for a K8s alert,
    which names a count and a namespace but rarely the pod."""
    target = kube.resolve_cluster(cluster)
    items = kube.pods_in_namespace(target, namespace)
    bad = [i for i in items if _is_unhealthy(i)]
    if not bad:
        return (f"No unhealthy pods in {cluster}"
                f"{'/' + namespace if namespace else ' (all namespaces)'} right now — "
                f"{len(items)} pod(s) checked, all Running/ready or completed. The condition "
                f"may have cleared since the alert fired; check the pods' restart counts and "
                f"ages before concluding it never happened.")
    # Worst first: highest restart count, so a 2000-restart crashlooper is not
    # buried under 40 one-off Error pods from a CronJob.
    def restarts(item: dict) -> int:
        return max((c.get("restartCount", 0)
                    for c in (item.get("status", {}).get("containerStatuses") or [])), default=0)
    bad.sort(key=restarts, reverse=True)
    shown = bad[:max(1, min(int(limit), 100))]
    lines = [f"{len(bad)} unhealthy pod(s) in {cluster}"
             f"{'/' + namespace if namespace else ' (all namespaces)'}"
             + (f", showing {len(shown)} worst by restart count:" if len(shown) < len(bad)
                else ":")]
    lines += [f"  {_pod_line(i)}" for i in shown]
    if len(shown) < len(bad):
        lines.append(f"  ... {len(bad) - len(shown)} more not shown — say so if you cite a count.")
    return "\n".join(lines)


def tool_get_pod_state(cluster: str, namespace: str, pod: str) -> str:
    """Everything about WHY this pod is not ready: phase, conditions, each
    container's state and previous-run exit code, and the pod's own events."""
    target = kube.resolve_cluster(cluster)
    item = kube.pod(target, namespace, pod)
    meta, status, spec = item.get("metadata", {}), item.get("status", {}), item.get("spec", {})

    lines = [f"pod: {meta.get('namespace')}/{meta.get('name')}",
             f"phase: {status.get('phase')}"
             + (f" (reason {status.get('reason')})" if status.get("reason") else ""),
             f"node: {spec.get('nodeName') or '(unscheduled)'}",
             f"age: {_age(meta.get('creationTimestamp', ''))}",
             f"owner: " + ", ".join(f"{o.get('kind')}/{o.get('name')}"
                                    for o in meta.get("ownerReferences") or []) or "owner: (none)"]

    conditions = status.get("conditions") or []
    not_true = [c for c in conditions if c.get("status") != "True"]
    if not_true:
        lines.append("failing conditions:")
        lines += [f"    {c.get('type')}={c.get('status')} reason={c.get('reason') or '-'} "
                  f"{(c.get('message') or '')[:200]}" for c in not_true]

    for kind, key in (("init containers", "initContainerStatuses"),
                      ("containers", "containerStatuses")):
        statuses = status.get(key) or []
        if statuses:
            lines.append(f"{kind}:")
            lines += [f"    {_container_summary(c)}" for c in statuses]

    events = kube.pod_events(target, namespace, pod)
    if events:
        lines.append("recent events (newest last):")
        for e in events[-8:]:
            lines.append(f"    {e.get('lastTimestamp') or e.get('eventTime') or '?'} "
                         f"{e.get('type')}/{e.get('reason')} x{e.get('count', 1)}: "
                         f"{' '.join((e.get('message') or '').split())[:220]}")
    else:
        lines.append("recent events: none returned (events age out after ~1h — absence is not "
                     "evidence the pod was healthy)")
    return "\n".join(lines)


def tool_get_pod_logs(cluster: str, namespace: str, pod: str, container: str = "",
                      previous: bool = False, tail: int = 50) -> str:
    """Container logs. For a CrashLoopBackOff pod call this with previous=true —
    the current container is usually seconds old, the previous one is the
    process that actually died."""
    target = kube.resolve_cluster(cluster)
    try:
        text = kube.pod_logs(target, namespace, pod, container=container,
                             previous=previous, tail=tail)
    except kube.KubectlError as e:
        # "previous terminated container not found" is the normal answer for a
        # pod that has never restarted — that IS information, not a failure.
        return (f"LOGS UNAVAILABLE ({'previous' if previous else 'current'} container): {e}\n"
                f"Do not describe log content you could not read.")
    text = text.strip()
    if not text:
        return (f"No {'previous-container ' if previous else ''}log output for "
                f"{namespace}/{pod}"
                + (f" container {container}" if container else "")
                + ". Empty is a real finding — say it, do not fill the gap.")
    header = (f"last {tail} line(s), {'PREVIOUS' if previous else 'current'} container, "
              f"{namespace}/{pod}" + (f" [{container}]" if container else ""))
    return (f"{header}\nSecrets are scrubbed automatically; quote lines verbatim otherwise.\n"
            f"---\n{text}")


def tool_list_replica_peers(cluster: str, namespace: str, pod: str) -> str:
    """Is it just this pod, or all of them?

    Walks pod -> owner (ReplicaSet/StatefulSet/DaemonSet/Job) -> that owner's
    other pods, and reports the workload's desired/ready/available counts
    alongside every sibling's state. One pod down out of six is a different
    incident from six out of six, and that distinction decides whether anyone
    needs to be woken up.
    """
    target = kube.resolve_cluster(cluster)
    item = kube.pod(target, namespace, pod)
    owners = item.get("metadata", {}).get("ownerReferences") or []
    if not owners:
        return (f"{namespace}/{pod} has no ownerReferences — it is a bare pod, so there is no "
                f"replica set to compare it against. Nothing will recreate it either.")

    owner = owners[0]
    owner_kind, owner_name, owner_uid = owner.get("kind"), owner.get("name"), owner.get("uid")
    lines = [f"{namespace}/{pod} is owned by {owner_kind}/{owner_name}"]

    # A ReplicaSet is itself owned by a Deployment; the Deployment is what a
    # human thinks of as "the service", so report both levels.
    try:
        owner_obj = kube.workload(target, owner_kind, namespace, owner_name)
    except kube.KubectlError as e:
        owner_obj = {}
        lines.append(f"  (could not read the {owner_kind}: {e})")

    grandparents = (owner_obj.get("metadata", {}).get("ownerReferences") or [])
    if grandparents:
        gp = grandparents[0]
        lines[0] += f", which belongs to {gp.get('kind')}/{gp.get('name')}"
        try:
            gp_obj = kube.workload(target, gp.get("kind"), namespace, gp.get("name"))
            st = gp_obj.get("status", {})
            lines.append(f"  {gp.get('kind')}/{gp.get('name')}: desired="
                         f"{gp_obj.get('spec', {}).get('replicas')} ready={st.get('readyReplicas', 0)} "
                         f"available={st.get('availableReplicas', 0)} updated="
                         f"{st.get('updatedReplicas', 0)}")
            for cond in st.get("conditions") or []:
                if cond.get("status") != "True":
                    lines.append(f"    condition {cond.get('type')}={cond.get('status')} "
                                 f"{cond.get('reason')}: "
                                 f"{' '.join((cond.get('message') or '').split())[:200]}")
        except kube.KubectlError as e:
            lines.append(f"  (could not read the {gp.get('kind')}: {e})")

    if owner_obj:
        st = owner_obj.get("status", {})
        # A DaemonSet has no spec.replicas — "desired" is one pod per matching
        # node, which only status knows. Reporting None there made a 41-node
        # DaemonSet look unconfigured.
        desired = (owner_obj.get("spec", {}).get("replicas")
                   if owner_obj.get("spec", {}).get("replicas") is not None
                   else st.get("desiredNumberScheduled"))
        lines.append(f"  {owner_kind}/{owner_name}: desired={desired} "
                     f"ready={st.get('readyReplicas', st.get('numberReady', 0))} "
                     f"current={st.get('replicas', st.get('currentNumberScheduled', 0))}")

    siblings = [p for p in kube.pods_in_namespace(target, namespace)
                if any(o.get("uid") == owner_uid
                       for o in p.get("metadata", {}).get("ownerReferences") or [])]
    healthy = [p for p in siblings if not _is_unhealthy(p)]
    unhealthy = [p for p in siblings if _is_unhealthy(p)]
    lines.append(f"  pods of this {owner_kind}: {len(siblings)} total, {len(healthy)} "
                 f"Running/ready, {len(unhealthy)} not.")
    # Show the alerting pod plus the worst peers, not all of them — a 41-node
    # DaemonSet does not need 41 lines to make the point, and the counts above
    # are what the thread actually quotes.
    def sort_key(p: dict) -> tuple:
        name = p["metadata"]["name"]
        restarts = max((c.get("restartCount", 0)
                        for c in (p.get("status", {}).get("containerStatuses") or [])), default=0)
        return (0 if name == pod else 1, -restarts)
    shown = sorted(unhealthy, key=sort_key)[:8]
    if not any(p["metadata"]["name"] == pod for p in shown):
        alerting = next((p for p in siblings if p["metadata"]["name"] == pod), None)
        if alerting:
            shown = [alerting] + shown[:7]
    for p in shown:
        flag = " <- the alerting pod" if p["metadata"]["name"] == pod else ""
        lines.append(f"   !! {_pod_line(p)}{flag}")
    if len(unhealthy) > len(shown):
        lines.append(f"   ... {len(unhealthy) - len(shown)} more unhealthy peer(s) not listed; "
                     f"the counts above are the whole picture.")
    for p in sorted(healthy, key=lambda p: p["metadata"]["name"])[:3]:
        lines.append(f"      {_pod_line(p)}  (healthy)")
    if len(siblings) == 1:
        lines.append("  Only one pod in this owner — there is no healthy peer to compare "
                     "against, so treat this as the whole workload being down.")
    return "\n".join(lines)


TOOLS = [
    {"name": "list_clusters",
     "description": "Which Kubernetes clusters this machine has a kubeconfig for. Call this "
                    "first if you are unsure the alert's cluster is reachable.",
     "inputSchema": {"type": "object", "properties": {}},
     "handler": tool_list_clusters},
    {"name": "list_unhealthy_pods",
     "description": "Pods that are not Running-and-ready, worst restart count first. Start "
                    "here: a K8s alert names a namespace and a count, not the pod. cluster is "
                    "the alert's environment key (e.g. aiw-stg198); namespace optional.",
     "inputSchema": {"type": "object", "properties": {
         "cluster": {"type": "string"}, "namespace": {"type": "string"},
         "limit": {"type": "integer"}}, "required": ["cluster"]},
     "handler": tool_list_unhealthy_pods},
    {"name": "get_pod_state",
     "description": "Why this pod is not ready: phase, failing conditions, each container's "
                    "state, restart count and PREVIOUS-run exit code, plus the pod's events.",
     "inputSchema": {"type": "object", "properties": {
         "cluster": {"type": "string"}, "namespace": {"type": "string"},
         "pod": {"type": "string"}}, "required": ["cluster", "namespace", "pod"]},
     "handler": tool_get_pod_state},
    {"name": "get_pod_logs",
     "description": "Container logs (secrets scrubbed, tail capped at 200). For a "
                    "CrashLoopBackOff pod pass previous=true — the current container is "
                    "usually seconds old, the previous one is what actually died.",
     "inputSchema": {"type": "object", "properties": {
         "cluster": {"type": "string"}, "namespace": {"type": "string"},
         "pod": {"type": "string"}, "container": {"type": "string"},
         "previous": {"type": "boolean"}, "tail": {"type": "integer"}},
         "required": ["cluster", "namespace", "pod"]},
     "handler": tool_get_pod_logs},
    {"name": "list_replica_peers",
     "description": "Is it one pod or all of them? Walks the pod's owner "
                    "(ReplicaSet/StatefulSet/DaemonSet) to the Deployment, reports "
                    "desired/ready/available and every sibling pod's state.",
     "inputSchema": {"type": "object", "properties": {
         "cluster": {"type": "string"}, "namespace": {"type": "string"},
         "pod": {"type": "string"}}, "required": ["cluster", "namespace", "pod"]},
     "handler": tool_list_replica_peers},
]

_HANDLERS = {t["name"]: t["handler"] for t in TOOLS}
_TOOL_SPECS = [{k: v for k, v in t.items() if k != "handler"} for t in TOOLS]


# ---------------------------------------------------------------- jsonrpc io

def _result(request_id: Any, payload: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle(request: dict) -> Optional[dict]:
    method, request_id = request.get("method"), request.get("id")
    if method == "initialize":
        return _result(request_id, {"protocolVersion": PROTOCOL_VERSION,
                                    "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO})
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": _TOOL_SPECS})
    if method == "tools/call":
        params = request.get("params") or {}
        handler = _HANDLERS.get(params.get("name"))
        if not handler:
            return _error(request_id, -32602, f"Unknown tool: {params.get('name')}")
        try:
            text = handler(**(params.get("arguments") or {}))
        except Exception as e:                          # noqa: BLE001 — the model adapts
            return _result(request_id, {"content": [{"type": "text",
                                                     "text": f"{type(e).__name__}: {e}"}],
                                        "isError": True})
        return _result(request_id, {"content": [{"type": "text", "text": text}]})
    if request_id is None:
        return None
    return _error(request_id, -32601, f"Method not found: {method}")


def serve() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        try:
            response = handle(request)
        except Exception as e:                          # noqa: BLE001
            response = _error(request.get("id"), -32603, f"{type(e).__name__}: {e}")
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


def _selftest(argv: List[str]) -> int:
    """Exercise every tool against a real cluster. Diagnostics go to stderr —
    stdout is protocol, and one stray print there makes the tools vanish."""
    cluster = next((a for a in argv if not a.startswith("-")), "")
    print(f"=== list_clusters ===\n{tool_list_clusters()}", file=sys.stderr)
    if not cluster:
        print("\nPass a cluster name to exercise the rest, e.g. --selftest aiw-stg198",
              file=sys.stderr)
        return 0
    print(f"\n=== list_unhealthy_pods {cluster} ===", file=sys.stderr)
    try:
        listing = tool_list_unhealthy_pods(cluster)
        print(listing[:1500], file=sys.stderr)
    except Exception as e:                              # noqa: BLE001
        print(f"FAILED {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    # Take the worst pod out of the listing and run the other three tools on it.
    worst = next((line.strip().split()[0] for line in listing.splitlines()
                  if "/" in line and line.startswith("  ")), "")
    if "/" not in worst:
        return 0
    namespace, name = worst.split("/", 1)
    for label, fn, kwargs in (
        ("get_pod_state", tool_get_pod_state, {}),
        ("list_replica_peers", tool_list_replica_peers, {}),
        ("get_pod_logs previous=True", tool_get_pod_logs, {"previous": True, "tail": 12}),
    ):
        print(f"\n=== {label} {namespace}/{name} ===", file=sys.stderr)
        try:
            print(fn(cluster=cluster, namespace=namespace, pod=name, **kwargs)[:1800],
                  file=sys.stderr)
        except Exception as e:                          # noqa: BLE001
            print(f"FAILED {type(e).__name__}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    if "--selftest" in sys.argv:
        sys.exit(_selftest([a for a in sys.argv[1:] if a != "--selftest"]))
    serve()
