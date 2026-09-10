#!/usr/bin/env python3
"""
Read-only kubectl access for the Kubernetes specialist.

Why this exists
---------------
Until now this specialist reasoned about pods through Prometheus/kube-state-
metrics, which can say "N pods are not ready" but never WHY. The owner's
direction (2026-08-27) is that a KubePodsNotReady/CrashLooping thread has to
carry three things a metric cannot give: the pod's actual STATE (waiting
reason, exit code, restart count), its LOGS (the crash message itself), and
whether the OTHER pods of the same replica set are running — one pod dead out
of six is a different incident from six out of six.

Credentials — a deliberate exception, same as Github_Agent
----------------------------------------------------------
CLAUDE.md non-negotiable #4 says credentials come from `.env`. This shells out
to the `kubectl` already installed on the box, against the kubeconfigs a human
generated with `~/eks/generate-kube-config.sh` (EKS + the AWS profile named in
each cluster's `.envrc`). Same reasoning already accepted for the `gh` CLI and
the `claude_cli` provider: the session belongs to a person, is refreshed by a
person, and no credential passes through a prompt. Nothing here mints, stores
or logs a token.

Safety properties, all enforced in code rather than asked of the model:
  * Five fixed read verbs. `get`, `describe`, `logs`, `events` — no `apply`,
    `delete`, `exec`, `scale`, `patch`, `port-forward`, and no way to reach one
    by argument, because verbs are literals in this file and never come from
    the caller.
  * Every caller-supplied name is validated against _NAME_RE before it reaches
    argv, so a "pod name" of `--as=cluster-admin` is rejected rather than
    becoming a flag. No shell is involved anywhere (`shell=False`, list argv).
  * Stage only, by default. KUBE_ENVIRONMENTS gates which ~/eks/<env>/
    directories may be used at all; prod requires an explicit opt-in even
    though every call here is a read.
  * Log output is scrubbed for obvious secrets before it can reach a Slack
    thread (see _scrub) — the ApiSuccessRate case in the Grafana agent's
    library already records raw upstream logs carrying live Bearer tokens.
"""
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

KUBECONFIG_DIR = Path(os.environ.get("KUBECONFIG_DIR", "~/eks")).expanduser()

# Which ~/eks subdirectories may be used. Stage only until the owner says
# otherwise — reads are safe, but a prod cluster named by a mis-parsed alert is
# still a surprise, and this is the cheapest place to make that impossible.
ALLOWED_ENVIRONMENTS = [e.strip() for e in
                        os.environ.get("KUBE_ENVIRONMENTS", "stage").split(",") if e.strip()]

# Kubernetes object names: RFC 1123-ish. The point is not validation for its own
# sake — it is that nothing starting with "-" can ever reach argv as a flag.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,252}$")

TIMEOUT_SECONDS = int(os.environ.get("KUBECTL_TIMEOUT", "60"))
MAX_LOG_LINES = 200

_SECRET_PATTERNS = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}"), r"\1<redacted>"),
    (re.compile(r"(?i)((?:password|passwd|secret|token|api[_-]?key)\"?\s*[:=]\s*\"?)"
                r"[^\s\"',]{6,}"), r"\1<redacted>"),
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), r"\1<redacted>"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
     "<redacted-jwt>"),
]


class KubectlError(RuntimeError):
    """A kubectl call that failed — surfaced to the model as text, never raised
    into the daemon. The message is what kubectl actually said, first lines
    only; a 30-line auth traceback in a Slack thread helps nobody."""


@dataclass
class Cluster:
    name: str
    environment: str
    kubeconfig: Path
    aws_profile: str = ""
    aws_region: str = ""


def _parse_envrc(path: Path) -> Dict[str, str]:
    """AWS_PROFILE/AWS_REGION out of the .envrc the generator script writes.

    Read rather than sourced: `direnv` is not on this process's path and a
    shell-out to source an arbitrary file is exactly the kind of thing this
    module exists to avoid.
    """
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        m = re.match(r"\s*export\s+(AWS_PROFILE|AWS_REGION)\s*=\s*(.+)\s*$", line)
        if m:
            values[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return values


def discover_clusters() -> List[Cluster]:
    """Every cluster this machine has a kubeconfig for, inside an allowed
    environment. Layout is the one generate-kube-config.sh produces:
    ~/eks/<environment>/<cluster>/kubeconfig_<cluster>.yaml
    """
    clusters: List[Cluster] = []
    if not KUBECONFIG_DIR.exists():
        return clusters
    for env_dir in sorted(KUBECONFIG_DIR.iterdir()):
        if not env_dir.is_dir() or env_dir.name not in ALLOWED_ENVIRONMENTS:
            continue
        for cluster_dir in sorted(env_dir.iterdir()):
            if not cluster_dir.is_dir():
                continue
            config = cluster_dir / f"kubeconfig_{cluster_dir.name}.yaml"
            if not config.exists():
                continue
            envrc = _parse_envrc(cluster_dir / ".envrc")
            clusters.append(Cluster(
                name=cluster_dir.name, environment=env_dir.name, kubeconfig=config,
                aws_profile=envrc.get("AWS_PROFILE", ""),
                aws_region=envrc.get("AWS_REGION", ""),
            ))
    return clusters


def resolve_cluster(name: str) -> Cluster:
    """A cluster by exact name, or a clear error naming what IS available.

    Deliberately exact: an alert's environment key is precise (`aiw-stg198`),
    and fuzzy-matching it onto a neighbouring cluster would put one cluster's
    pod states in another cluster's incident thread.
    """
    clusters = discover_clusters()
    if not clusters:
        raise KubectlError(
            f"No kubeconfigs found under {KUBECONFIG_DIR} for environment(s) "
            f"{', '.join(ALLOWED_ENVIRONMENTS)}. Nothing to query — say so rather than "
            f"describing pods you cannot see.")
    for cluster in clusters:
        if cluster.name == name:
            return cluster
    raise KubectlError(
        f"No kubeconfig for cluster {name!r}. Available ({', '.join(ALLOWED_ENVIRONMENTS)}): "
        + ", ".join(f"{c.name} [{c.environment}]" for c in clusters)
        + ". This alert's cluster is not reachable from here — say that plainly.")


def _validate(kind: str, value: str) -> str:
    if not _NAME_RE.match(value or ""):
        raise KubectlError(f"Refusing {kind}={value!r}: not a valid Kubernetes name. "
                           f"Names are lower-case alphanumerics with '-', '.', '_'.")
    return value


def _scrub(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def run(cluster: Cluster, args: List[str], timeout: Optional[int] = None) -> str:
    """One kubectl invocation. `args` is built by this module only — never
    passed through from a tool argument, so no caller can add a flag."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", ""),
        "KUBECONFIG": str(cluster.kubeconfig),
    }
    # The EKS kubeconfig's exec credential plugin needs these; the .envrc a
    # human already wrote per cluster is the source of truth for which.
    if cluster.aws_profile:
        env["AWS_PROFILE"] = cluster.aws_profile
    if cluster.aws_region:
        env["AWS_REGION"] = cluster.aws_region
    for passthrough in ("AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE", "AWS_CA_BUNDLE",
                        "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE"):
        if os.environ.get(passthrough):
            env[passthrough] = os.environ[passthrough]

    seconds = timeout or TIMEOUT_SECONDS
    cmd = ["kubectl", *args, f"--request-timeout={seconds}s"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 15,
                              env=env, stdin=subprocess.DEVNULL, shell=False)
    except FileNotFoundError as e:
        raise KubectlError("kubectl is not installed on this machine") from e
    except subprocess.TimeoutExpired as e:
        raise KubectlError(f"kubectl timed out after {seconds}s against {cluster.name}") from e
    if proc.returncode != 0:
        detail = " ".join((proc.stderr or proc.stdout or "").split())[:400]
        # An expired AWS session is the common failure and reads as a permission
        # error; name it so the model reports "cannot see the cluster" instead of
        # inventing a Kubernetes explanation.
        if "expired" in detail.lower() or "credential" in detail.lower():
            detail += (f" — the AWS session for profile {cluster.aws_profile or '?'} looks "
                       f"expired; a human has to refresh it.")
        raise KubectlError(f"kubectl {' '.join(args[:3])} failed on {cluster.name}: {detail}")
    return proc.stdout


def get_json(cluster: Cluster, args: List[str]) -> dict:
    import json
    raw = run(cluster, [*args, "-o", "json"])
    try:
        return json.loads(raw)
    except ValueError as e:
        raise KubectlError(f"kubectl returned output that is not JSON: {raw[:200]!r}") from e


# ------------------------------------------------------------------ read verbs

def pod(cluster: Cluster, namespace: str, name: str) -> dict:
    return get_json(cluster, ["get", "pod", _validate("pod", name),
                              "-n", _validate("namespace", namespace)])


def pods_in_namespace(cluster: Cluster, namespace: str = "") -> List[dict]:
    args = ["get", "pods"]
    args += ["-n", _validate("namespace", namespace)] if namespace else ["-A"]
    return get_json(cluster, args).get("items") or []


def workload(cluster: Cluster, kind: str, namespace: str, name: str) -> dict:
    if kind.lower() not in ("replicaset", "deployment", "statefulset", "daemonset", "job",
                            "cronjob"):
        raise KubectlError(f"Refusing to read kind={kind!r}: not a workload kind")
    return get_json(cluster, ["get", kind.lower(), _validate("name", name),
                              "-n", _validate("namespace", namespace)])


def pod_events(cluster: Cluster, namespace: str, name: str) -> List[dict]:
    """Events for one pod. Field-selected server-side rather than fetching the
    namespace's events and filtering here — a busy namespace has thousands."""
    data = get_json(cluster, [
        "get", "events", "-n", _validate("namespace", namespace),
        f"--field-selector=involvedObject.name={_validate('pod', name)}",
    ])
    return data.get("items") or []


def pod_logs(cluster: Cluster, namespace: str, name: str, container: str = "",
             previous: bool = False, tail: int = 50) -> str:
    """Container logs, scrubbed and capped.

    `previous=True` is the one that matters for a CrashLoopBackOff: the current
    container is usually seconds old or not started, while the PREVIOUS one is
    the process that actually died and holds the stack trace.
    """
    tail = max(1, min(int(tail), MAX_LOG_LINES))
    args = ["logs", _validate("pod", name), "-n", _validate("namespace", namespace),
            f"--tail={tail}"]
    if container:
        args += ["-c", _validate("container", container)]
    if previous:
        args.append("--previous")
    return _scrub(run(cluster, args))
