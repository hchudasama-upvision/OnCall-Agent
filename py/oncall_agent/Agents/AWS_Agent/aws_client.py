#!/usr/bin/env python3
"""
Read-only AWS access for the AWS specialist — RDS, CloudWatch, Performance
Insights, via the `aws` CLI already authenticated on this box.

Credentials — the same deliberate exception as gh/kubectl
---------------------------------------------------------
CLAUDE.md non-negotiable #4 says credentials come from `.env`. This shells out
to the `aws` CLI using the operator's own SSO session (`aws sso login`), the
same reasoning already accepted for the `gh` CLI, `kubectl`, and the
`claude_cli` provider: the session belongs to a person, is refreshed by a
person, and no credential ever passes through a prompt. Nothing here mints,
stores or prints a token.

Why the CLI and not boto3
-------------------------
boto3 is not installed and this repo is deliberately dependency-light
(CLAUDE.md Style). The CLI is already here, already carries the SSO session,
and is what the ~/eks scripts use — one auth story instead of two.

Safety properties, all in code rather than asked of the model:
  * An ALLOW-LIST of read-only subcommands (_READ_COMMANDS). `rds
    describe-db-instances` is allowed; `rds reboot-db-instance`, `modify-*`,
    `delete-*` are not reachable at all, because the verb pair is matched
    against that list before argv is built.
  * Every caller-supplied identifier is validated (_NAME_RE) before it reaches
    argv, so an "instance name" of `--profile=other` is refused rather than
    becoming a flag. No shell anywhere (`shell=False`, list argv).
  * Profiles are themselves allow-listed via AWS_PROFILES, so an alert cannot
    steer the agent into an account nobody intended it to read.
"""
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# Which profiles may be used at all, and where. Read from the environment on
# EVERY call, not at import time: follow_up.py imports this module while the
# handler is still being imported, which can be before load_dotenv() has run —
# a module-level read then froze AWS_PROFILES as empty and the +5min RDS
# re-check went looking in the `default` profile (real failure, 2026-08-31).
def _env_list(name: str, default: str = "") -> list:
    return [v.strip() for v in os.environ.get(name, default).split(",") if v.strip()]


def profiles() -> List[str]:
    """The allow-list, or the ambient profile when none is configured."""
    return _env_list("AWS_PROFILES") or [os.environ.get("AWS_PROFILE", "default")]


def regions() -> List[str]:
    return _env_list("AWS_REGIONS", "us-east-1")


def timeout_seconds() -> int:
    return int(os.environ.get("AWS_CLI_TIMEOUT", "90"))

# (service, subcommand) pairs this module may ever run. Every one is a read.
_READ_COMMANDS = {
    ("rds", "describe-db-instances"),
    ("rds", "describe-db-clusters"),
    ("rds", "describe-events"),
    ("rds", "describe-db-log-files"),
    ("cloudwatch", "get-metric-statistics"),
    ("cloudwatch", "get-metric-data"),
    ("cloudwatch", "get-metric-widget-image"),
    ("cloudwatch", "describe-alarms"),
    ("cloudwatch", "describe-alarm-history"),
    ("cloudwatch", "list-metrics"),
    ("elbv2", "describe-load-balancers"),
    ("elbv2", "describe-target-groups"),
    ("elbv2", "describe-target-health"),
    ("pi", "describe-dimension-keys"),
    ("pi", "get-resource-metrics"),
    ("sts", "get-caller-identity"),
}

# AWS identifiers: RDS names, DbiResourceIds, regions, profile names. The point
# is that nothing beginning with "-" can reach argv as a flag.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


class AwsError(RuntimeError):
    """A failed AWS call, surfaced to the model as text rather than raised into
    the daemon. Expired SSO is named explicitly, because it looks like a
    permissions error and would otherwise be reported as one."""


@dataclass
class Account:
    profile: str
    account_id: str = ""
    arn: str = ""


def _validate(kind: str, value: str) -> str:
    if not _NAME_RE.match(value or ""):
        raise AwsError(f"Refusing {kind}={value!r}: not a valid AWS identifier.")
    return value


def run(service: str, subcommand: str, args: Optional[List[str]] = None,
        profile: str = "", region: str = "", raw: bool = False) -> Any:
    """One read-only aws CLI call. `service`/`subcommand` must be allow-listed.

    Returns parsed JSON, or the raw stdout string when raw=True (which is how
    get-metric-widget-image's base64 payload comes back).
    """
    if (service, subcommand) not in _READ_COMMANDS:
        raise AwsError(f"Refusing `aws {service} {subcommand}`: not in the read-only "
                       f"allow-list. This surface cannot change anything in AWS.")
    profile = profile or profiles()[0]
    allowed = _env_list("AWS_PROFILES")
    if allowed and profile not in allowed:
        raise AwsError(f"Profile {profile!r} is not in AWS_PROFILES "
                       f"({', '.join(allowed)}) — refusing to read an account "
                       f"nobody configured.")
    cmd = ["aws", service, subcommand, "--profile", _validate("profile", profile)]
    if region:
        cmd += ["--region", _validate("region", region)]
    cmd += list(args or [])
    if not raw:
        cmd += ["--output", "json"]

    env = {k: v for k, v in os.environ.items()
           if k in ("PATH", "HOME", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE",
                    "AWS_CA_BUNDLE", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE")}
    # CloudWatch throttles, GetMetricWidgetImage especially — a real render of
    # the Rekognition throttling alarm came back "Throttling: Rate exceeded"
    # (2026-08-31), which would otherwise have been reported to the thread as
    # "no graph available". Retry the transient ones only; an auth failure or a
    # missing resource is not retried, because repeating it just wastes the
    # incident's time.
    detail = ""
    for attempt in range(3):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                                  timeout=timeout_seconds(), stdin=subprocess.DEVNULL,
                                  shell=False)
        except FileNotFoundError as e:
            raise AwsError("the `aws` CLI is not installed on this machine") from e
        except subprocess.TimeoutExpired as e:
            raise AwsError(f"`aws {service} {subcommand}` timed out after "
                           f"{timeout_seconds()}s") from e
        if proc.returncode == 0:
            break
        detail = " ".join((proc.stderr or proc.stdout or "").split())[:400]
        transient = any(marker in detail for marker in
                        ("Throttling", "Rate exceeded", "RequestLimitExceeded",
                         "ServiceUnavailable", "InternalError", "TooManyRequests"))
        if not transient or attempt == 2:
            break
        time.sleep(2 + 3 * attempt)
    if proc.returncode != 0:
        low = detail.lower()
        if "sso" in low or "expired" in low or "ForbiddenException" in detail:
            detail += (f" — the credentials for profile {profile!r} look expired. For an SSO "
                       f"profile a human runs `aws sso login --profile {profile}`; for a "
                       f"key/role profile the keys or the assume-role need attention. Do not "
                       f"guess the numbers.")
        elif any(m in detail for m in ("Throttling", "Rate exceeded")):
            detail += (" — AWS rate-limited this call after 3 attempts. That is a gap in the "
                       "evidence, not a reading; say the graph/metric was unavailable.")
        raise AwsError(f"`aws {service} {subcommand}` failed on {profile}: {detail}")
    if raw:
        return proc.stdout
    try:
        return json.loads(proc.stdout or "{}")
    except ValueError as e:
        raise AwsError(f"aws returned output that is not JSON: {proc.stdout[:200]!r}") from e


def whoami(profile: str) -> Account:
    data = run("sts", "get-caller-identity", profile=profile)
    return Account(profile=profile, account_id=data.get("Account", ""), arn=data.get("Arn", ""))


def find_db_instance(identifier: str) -> Dict[str, Any]:
    """Locate one RDS instance across the allowed profiles and regions.

    An alert names the database, not the account. Searching rather than assuming
    is what stops the agent reporting metrics from a same-named instance in the
    wrong account — and the result says which profile/region/account it found,
    so the thread can state it.
    """
    _validate("db instance", identifier)
    misses = []
    for profile in profiles():
        for region in regions():
            try:
                data = run("rds", "describe-db-instances",
                           ["--db-instance-identifier", identifier],
                           profile=profile, region=region)
            except AwsError as e:
                misses.append(f"{profile}/{region}: {str(e)[:120]}")
                continue
            items = data.get("DBInstances") or []
            if items:
                instance = items[0]
                instance["_profile"] = profile
                instance["_region"] = region
                return instance
    raise AwsError(f"RDS instance {identifier!r} not found in "
                   f"{', '.join(profiles())} / {', '.join(regions())}. "
                   + ("Details: " + " | ".join(misses[:3]) if misses else "")
                   + " Do not report metrics for an instance you could not locate.")


def list_db_instances(profile: str = "", region: str = "") -> List[Dict[str, Any]]:
    out = []
    for prof in ([profile] if profile else profiles()):
        for reg in ([region] if region else regions()):
            try:
                data = run("rds", "describe-db-instances", profile=prof, region=reg)
            except AwsError:
                continue
            for item in data.get("DBInstances") or []:
                item["_profile"], item["_region"] = prof, reg
                out.append(item)
    return out


def find_alarm(name: str) -> Dict[str, Any]:
    """Locate one CloudWatch alarm across the allowed profiles and regions.

    Same "search, don't assume" rule as find_db_instance, and it matters more
    here: this repo now spans commercial AWS (SSO, us-east-1) and GovCloud
    (static keys assuming a role, us-gov-west-1). The alarm name in the alert
    (e.g. Rekognition-ThrottledCount-High-wpsc01) says which environment it is
    about but nothing about which partition it lives in, and an alarm name is
    not unique across accounts.
    """
    _validate("alarm", name)
    misses = []
    for profile in profiles():
        for region in regions():
            try:
                data = run("cloudwatch", "describe-alarms", ["--alarm-names", name],
                           profile=profile, region=region)
            except AwsError as e:
                misses.append(f"{profile}/{region}: {str(e)[:100]}")
                continue
            for alarm in data.get("MetricAlarms") or []:
                alarm["_profile"], alarm["_region"] = profile, region
                return alarm
    raise AwsError(f"CloudWatch alarm {name!r} not found in {', '.join(profiles())} / "
                   f"{', '.join(regions())}. "
                   + ("Details: " + " | ".join(misses[:3]) if misses else "")
                   + " Do not describe an alarm you could not read.")


def find_load_balancer(name_or_arn: str) -> Dict[str, Any]:
    """Locate one load balancer across the allowed profiles and regions.

    The alert's summary carries the ARN tail — "app/uk-prod-fastcore-app-http/
    992c86a8ba9f5fb3" — so the NAME is the middle segment. Searching rather than
    assuming matters here as much as it does for RDS: this repo spans several
    accounts and two partitions, and `uk-prod` is eu-west-2 while everything
    else so far has been us-east-1 or us-gov-west-1.
    """
    name = name_or_arn.strip()
    if "/" in name:                     # app/<name>/<id> or a full ARN
        parts = [p for p in name.split("/") if p]
        name = parts[1] if parts[0] in ("app", "net") and len(parts) > 1 else parts[-2] \
            if len(parts) > 2 else parts[-1]
    _validate("load balancer", name)
    misses = []
    for profile in profiles():
        for region in regions():
            try:
                data = run("elbv2", "describe-load-balancers", ["--names", name],
                           profile=profile, region=region)
            except AwsError as e:
                misses.append(f"{profile}/{region}: {str(e)[:90]}")
                continue
            for balancer in data.get("LoadBalancers") or []:
                balancer["_profile"], balancer["_region"] = profile, region
                # The CloudWatch dimension is the ARN tail, not the name.
                balancer["_dimension"] = balancer["LoadBalancerArn"].split("loadbalancer/")[-1]
                return balancer
    raise AwsError(f"load balancer {name!r} not found in {', '.join(profiles())} / "
                   f"{', '.join(regions())}. "
                   + ("Details: " + " | ".join(misses[:3]) if misses else "")
                   + " Do not describe target health you could not read.")


def target_groups_for(balancer: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The target groups attached to this load balancer, with their CloudWatch
    dimension values filled in."""
    data = run("elbv2", "describe-target-groups",
               ["--load-balancer-arn", balancer["LoadBalancerArn"]],
               profile=balancer["_profile"], region=balancer["_region"])
    groups = data.get("TargetGroups") or []
    for group in groups:
        group["_dimension"] = group["TargetGroupArn"].split(":")[-1]
    return groups


def target_health(balancer: Dict[str, Any], target_group_arn: str) -> List[Dict[str, Any]]:
    data = run("elbv2", "describe-target-health",
               ["--target-group-arn", target_group_arn],
               profile=balancer["_profile"], region=balancer["_region"])
    return data.get("TargetHealthDescriptions") or []
