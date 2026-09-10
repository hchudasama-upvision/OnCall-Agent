#!/usr/bin/env python3
"""
AWS tools for the investigating model — RDS, CloudWatch, Performance Insights.

The owner's direction (2026-08-31): alerts like
    [FIRING:1] us-1 : prod - RDS_CPUUtilizationAvgCriticalCore
get their own AWS specialist, and the thread has to carry
    the GRAPHS   -> rds_metric_graph (CPUUtilization AND DatabaseConnections)
    the NUMBERS  -> rds_metrics
    the TOP SQL  -> rds_top_queries (Performance Insights)
plus a usage graph again 5-10 minutes later, which follow_up.py owns.

Why CloudWatch renders the picture, not a browser
-------------------------------------------------
`cloudwatch get-metric-widget-image` renders a graph PNG server-side, so this
needs no Playwright, no login session and no waiting for a panel to settle —
the whole class of "screenshotted the word Loading" bugs that the Grafana path
had to solve does not exist here. It returns base64; that is decoded and
written to the run's evidence directory with a manifest entry, exactly like
Grafana_Agent's render_panel, so the same attach-by-key mechanism works.

Everything is read-only, enforced in aws_client.py by an allow-list of
(service, subcommand) pairs — `reboot-db-instance` and every `modify-*` are
not reachable from here at all.
"""
import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ... import chart
from . import aws_client as aws

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "noc-aws", "version": "0.1.0"}

EVIDENCE_DIR = Path(os.environ.get("EVIDENCE_DIR")
                    or Path(__file__).resolve().parents[4] / "dist" / "evidence")
MANIFEST = "rendered-panels.json"      # shared with the Grafana server on purpose:
                                       # registry.py reads one manifest per run.

# The metrics that answer an RDS CPU/connection alert, with the units a human
# expects to see them in. Kept here rather than asked of the model so a thread
# cannot end up quoting FreeableMemory in bytes as if it were a percentage.
_METRICS = {
    "CPUUtilization": {"unit": "Percent", "stat": "Average"},
    "DatabaseConnections": {"unit": "Count", "stat": "Average"},
    "FreeableMemory": {"unit": "Bytes", "stat": "Average"},
    "ReadIOPS": {"unit": "Count/Second", "stat": "Average"},
    "WriteIOPS": {"unit": "Count/Second", "stat": "Average"},
    "ReadLatency": {"unit": "Seconds", "stat": "Average"},
    "WriteLatency": {"unit": "Seconds", "stat": "Average"},
    "DiskQueueDepth": {"unit": "Count", "stat": "Average"},
}


def _record(key: str, path: Path, caption: str) -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = EVIDENCE_DIR / MANIFEST
    data = {}
    if manifest_path.exists():
        try:
            data = json.loads(manifest_path.read_text())
        except ValueError:
            data = {}
    data[key] = {"path": str(path), "caption": caption}
    manifest_path.write_text(json.dumps(data, indent=2))


def _human_bytes(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024:
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}PB"


# --------------------------------------------------------------------- tools

def tool_list_rds_instances(name_filter: str = "") -> str:
    """Every RDS instance the configured profiles can see — the entry point when
    an alert names an environment rather than a database."""
    try:
        items = aws.list_db_instances()
    except aws.AwsError as e:
        return f"LOOKUP FAILED — {e}"
    if not items:
        return (f"No RDS instances visible in profiles {', '.join(aws.profiles())} / regions "
                f"{', '.join(aws.regions())}. Either the SSO session expired or "
                f"AWS_PROFILES points at the wrong account — say so rather than guessing.")
    lines = []
    for item in items:
        name = item.get("DBInstanceIdentifier", "?")
        if name_filter and name_filter.lower() not in name.lower():
            continue
        lines.append(f"{name}  [{item.get('_profile')}/{item.get('_region')}]  "
                     f"{item.get('Engine')} {item.get('EngineVersion')}  "
                     f"{item.get('DBInstanceClass')}  status={item.get('DBInstanceStatus')}  "
                     f"PI={'on' if item.get('PerformanceInsightsEnabled') else 'off'}")
    if not lines:
        return f"No RDS instance matches {name_filter!r} in {', '.join(aws.profiles())}."
    return "\n".join(sorted(lines))


def tool_rds_instance_summary(instance: str) -> str:
    """Where this database lives and what it is: account, region, class, engine,
    Multi-AZ, storage, and whether Performance Insights can answer top-SQL."""
    try:
        item = aws.find_db_instance(instance)
        account = aws.whoami(item["_profile"])
    except aws.AwsError as e:
        return f"LOOKUP FAILED — {e}"
    return "\n".join([
        f"instance: {item.get('DBInstanceIdentifier')}",
        f"account:  {account.account_id} (profile {item['_profile']})",
        f"region:   {item['_region']}  az={item.get('AvailabilityZone')}  "
        f"multi_az={item.get('MultiAZ')}",
        f"engine:   {item.get('Engine')} {item.get('EngineVersion')} on "
        f"{item.get('DBInstanceClass')}",
        f"status:   {item.get('DBInstanceStatus')}",
        f"storage:  {item.get('AllocatedStorage')}GB {item.get('StorageType')}",
        f"performance insights: "
        + ("enabled — rds_top_queries can show the top SQL"
           if item.get("PerformanceInsightsEnabled")
           else "DISABLED — top SQL is not available for this instance; say that rather "
                "than speculating about which query is responsible"),
        f"dbi_resource_id: {item.get('DbiResourceId')}",
    ])


def tool_rds_metrics(instance: str, hours: int = 3,
                     metrics: Optional[List[str]] = None) -> str:
    """Current and peak values for the metrics that matter on a CPU alert.

    The numbers go in the thread text; the graph is a separate call. Reading
    them here rather than off a picture is what keeps the post exact.
    """
    try:
        item = aws.find_db_instance(instance)
    except aws.AwsError as e:
        return f"LOOKUP FAILED — {e}"
    names = metrics or ["CPUUtilization", "DatabaseConnections", "FreeableMemory",
                        "ReadIOPS", "WriteIOPS", "DiskQueueDepth"]
    hours = max(1, min(int(hours), 336))
    end = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours * 3600))
    lines = [f"{item.get('DBInstanceIdentifier')} [{item['_profile']}/{item['_region']}] "
             f"over the last {hours}h ({start} -> {end}):"]
    for name in names:
        spec = _METRICS.get(name)
        if not spec:
            lines.append(f"  {name}: not in the known metric list — skipped")
            continue
        try:
            data = aws.run("cloudwatch", "get-metric-statistics", [
                "--namespace", "AWS/RDS", "--metric-name", name,
                "--dimensions", f"Name=DBInstanceIdentifier,Value={item['DBInstanceIdentifier']}",
                "--start-time", start, "--end-time", end,
                "--period", "300", "--statistics", "Average", "Maximum",
            ], profile=item["_profile"], region=item["_region"])
        except aws.AwsError as e:
            lines.append(f"  {name}: unavailable ({str(e)[:120]})")
            continue
        points = sorted(data.get("Datapoints") or [], key=lambda p: p.get("Timestamp", ""))
        if not points:
            lines.append(f"  {name}: no datapoints in this window")
            continue
        latest, peak = points[-1], max(points, key=lambda p: p.get("Maximum", 0))
        if spec["unit"] == "Bytes":
            lines.append(f"  {name}: now {_human_bytes(latest.get('Average', 0))}, "
                         f"low {_human_bytes(min(p.get('Average', 0) for p in points))}")
        else:
            lines.append(f"  {name}: now {latest.get('Average', 0):.2f}, "
                         f"peak {peak.get('Maximum', 0):.2f} "
                         f"(avg {sum(p.get('Average', 0) for p in points) / len(points):.2f}) "
                         f"{spec['unit']}")
    return "\n".join(lines)


def tool_rds_metric_graph(instance: str, metrics: Optional[List[str]] = None,
                          hours: int = 3, key: str = "", title: str = "") -> str:
    """Render a CloudWatch graph PNG for this instance and return its evidence key.

    CloudWatch draws it server-side, so there is no browser and no settle-time
    problem. Pass one metric per graph for a readable picture — CPUUtilization
    and DatabaseConnections have different units and do not belong on one axis.
    """
    try:
        item = aws.find_db_instance(instance)
    except aws.AwsError as e:
        return f"LOOKUP FAILED — {e}"
    names = [m for m in (metrics or ["CPUUtilization"]) if m in _METRICS]
    if not names:
        return (f"None of {metrics!r} is a known RDS metric. Known: "
                f"{', '.join(sorted(_METRICS))}")
    hours = max(1, min(int(hours), 336))
    identifier = item["DBInstanceIdentifier"]
    widget = {
        "metrics": [["AWS/RDS", name, "DBInstanceIdentifier", identifier,
                     {"label": name, "stat": _METRICS[name]["stat"]}] for name in names],
        "period": 300 if hours <= 12 else 900,
        "region": item["_region"],
        "title": title or f"{identifier} — {', '.join(names)} (last {hours}h)",
        "width": 1100, "height": 340, "view": "timeSeries",
        "start": f"-PT{hours}H", "end": "P0D",
        "yAxis": {"left": {"min": 0}},
    }
    key = key or f"aws_rds_{identifier}_{'_'.join(names)}".replace("-", "_").lower()
    out_path = EVIDENCE_DIR / f"{key}.png"
    try:
        payload = aws.run("cloudwatch", "get-metric-widget-image",
                          ["--metric-widget", json.dumps(widget), "--output", "text"],
                          profile=item["_profile"], region=item["_region"], raw=True)
        image = base64.b64decode(payload.strip())
    except Exception as e:                              # noqa: BLE001
        return f"RENDER FAILED — {type(e).__name__}: {str(e).splitlines()[0]}"
    if not image.startswith(b"\x89PNG"):
        return ("RENDER FAILED — CloudWatch did not return a PNG. Nothing was saved; do not "
                "describe a graph you do not have.")
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(image)
    caption = f"CloudWatch {identifier} · {', '.join(names)} · last {hours}h · {item['_region']}"
    _record(key, out_path, caption)
    return (f"OK — rendered {len(image)} bytes.\nevidence_key: {key}\ncaption: {caption}\n"
            f"Cite \"{key}\" in a post's evidence_keys to attach it.")


def tool_rds_top_queries(instance: str, hours: int = 1, limit: int = 5) -> str:
    """Top SQL by database load, and what the database is waiting on.

    Performance Insights returns TOKENIZED statements ($1, $2 in place of
    values), so no literal customer data comes back with them — but they are
    long, so quote the shape of the statement, not the whole thing.
    """
    try:
        item = aws.find_db_instance(instance)
    except aws.AwsError as e:
        return f"LOOKUP FAILED — {e}"
    if not item.get("PerformanceInsightsEnabled"):
        return (f"Performance Insights is DISABLED on {item.get('DBInstanceIdentifier')} — "
                f"top SQL is not available. Say that; do not speculate about which query is "
                f"responsible.")
    resource_id = item.get("DbiResourceId")
    hours = max(1, min(int(hours), 24))
    end = int(time.time())
    start = end - hours * 3600
    out = [f"{item.get('DBInstanceIdentifier')} — Performance Insights, last {hours}h "
           f"(db.load.avg; 1.0 means one active session on average):"]
    for group, heading in (("db.sql_tokenized", "TOP SQL BY LOAD"),
                           ("db.wait_event", "TOP WAIT EVENTS")):
        try:
            data = aws.run("pi", "describe-dimension-keys", [
                "--service-type", "RDS", "--identifier", resource_id,
                "--start-time", str(start), "--end-time", str(end),
                "--metric", "db.load.avg",
                "--group-by", json.dumps({"Group": group, "Limit": max(1, min(int(limit), 10))}),
            ], profile=item["_profile"], region=item["_region"])
        except aws.AwsError as e:
            out.append(f"\n{heading}: unavailable ({str(e)[:150]})")
            continue
        keys = data.get("Keys") or []
        if not keys:
            out.append(f"\n{heading}: nothing returned for this window")
            continue
        out.append(f"\n{heading}:")
        for entry in keys:
            dims = entry.get("Dimensions") or {}
            load = entry.get("Total") or 0.0
            if group == "db.wait_event":
                out.append(f"  {load:7.3f}  {dims.get('db.wait_event.name', '?')} "
                           f"[{dims.get('db.wait_event.type', '?')}]")
                continue
            statement = " ".join((dims.get("db.sql_tokenized.statement") or "").split())
            out.append(f"  {load:7.3f}  id={dims.get('db.sql_tokenized.id', '?')[:12]}  "
                       f"{statement[:220]}")
    out.append("\nStatements are tokenized by AWS ($1, $2 replace literal values). Quote them "
               "as returned; do not reconstruct a real query.")
    return "\n".join(out)


def tool_rds_events(instance: str, hours: int = 24) -> str:
    """Recent RDS events — failover, reboot, storage autoscaling, parameter
    changes. Often the actual answer, and cheap to check."""
    try:
        item = aws.find_db_instance(instance)
        data = aws.run("rds", "describe-events", [
            "--source-identifier", item["DBInstanceIdentifier"],
            "--source-type", "db-instance",
            "--duration", str(max(1, min(int(hours), 336)) * 60),
        ], profile=item["_profile"], region=item["_region"])
    except aws.AwsError as e:
        return f"LOOKUP FAILED — {e}"
    events = data.get("Events") or []
    if not events:
        return (f"No RDS events for {item.get('DBInstanceIdentifier')} in the last {hours}h. "
                f"That rules out a failover/reboot/parameter change in that window; it says "
                f"nothing about workload.")
    return "\n".join([f"{item.get('DBInstanceIdentifier')} events, last {hours}h:"]
                     + [f"  {e.get('Date')} [{','.join(e.get('EventCategories') or [])}] "
                        f"{e.get('Message')}" for e in events[-12:]])


def _render_widget(widget: dict, key: str, caption: str, profile: str, region: str) -> str:
    """Render one CloudWatch metric widget to a PNG and record it as evidence.

    CloudWatch draws the graph server-side (get-metric-widget-image), so there
    is no browser, no login session and no settle-time polling — none of the
    "screenshotted the spinner" class of bug the Grafana path had to solve.
    """
    out_path = EVIDENCE_DIR / f"{key}.png"
    try:
        payload = aws.run("cloudwatch", "get-metric-widget-image",
                          ["--metric-widget", json.dumps(widget), "--output", "text"],
                          profile=profile, region=region, raw=True)
        image = base64.b64decode(payload.strip())
    except Exception as e:                              # noqa: BLE001
        return f"RENDER FAILED — {type(e).__name__}: {str(e).splitlines()[0]}"
    if not image.startswith(b"\x89PNG"):
        return ("RENDER FAILED — CloudWatch did not return a PNG. Nothing was saved; do not "
                "describe a graph you do not have.")
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(image)
    _record(key, out_path, caption)
    return (f"OK — rendered {len(image)} bytes.\nevidence_key: {key}\ncaption: {caption}\n"
            f"Cite \"{key}\" in a post's evidence_keys to attach it.")


def tool_cloudwatch_alarm(name: str) -> str:
    """What a CloudWatch alarm actually watches, its state, and when it last changed.

    The entry point for any alert whose name IS an alarm name (e.g.
    Rekognition-ThrottledCount-High-wpsc01): it gives the namespace, metric,
    statistic, period, threshold and comparison, so the thread can say what the
    condition really is instead of restating the alarm's name back.
    """
    try:
        alarm = aws.find_alarm(name)
        account = aws.whoami(alarm["_profile"])
    except aws.AwsError as e:
        return f"LOOKUP FAILED — {e}"
    dims = alarm.get("Dimensions") or []
    lines = [
        f"alarm:     {alarm.get('AlarmName')}",
        f"account:   {account.account_id} (profile {alarm['_profile']}, "
        f"{'GovCloud' if 'us-gov' in alarm['_region'] else 'commercial'})",
        f"region:    {alarm['_region']}",
        f"condition: {alarm.get('Namespace')} {alarm.get('MetricName')} "
        f"{alarm.get('Statistic') or alarm.get('ExtendedStatistic')} over "
        f"{alarm.get('Period')}s {alarm.get('ComparisonOperator')} {alarm.get('Threshold')} "
        f"for {alarm.get('EvaluationPeriods')} period(s)",
        f"dimensions: " + (", ".join(f"{d['Name']}={d['Value']}" for d in dims)
                           if dims else "(none — this alarm is account-wide for that metric, "
                                        "not scoped to one resource)"),
        f"state:     {alarm.get('StateValue')} since {alarm.get('StateUpdatedTimestamp')}",
        f"reason:    {' '.join((alarm.get('StateReason') or '').split())[:300]}",
    ]
    if alarm.get("StateValue") == "INSUFFICIENT_DATA":
        lines.append("NOTE: INSUFFICIENT_DATA means the metric reported nothing in the window, "
                     "which is NOT the same as a healthy zero. Say which it is only if you "
                     "check the metric itself.")
    try:
        history = aws.run("cloudwatch", "describe-alarm-history",
                          ["--alarm-name", alarm["AlarmName"],
                           "--history-item-type", "StateUpdate", "--max-records", "6"],
                          profile=alarm["_profile"], region=alarm["_region"])
        items = history.get("AlarmHistoryItems") or []
        if items:
            lines.append("recent state changes (newest first):")
            lines += [f"    {i.get('Timestamp')} {i.get('HistorySummary')}" for i in items[:6]]
    except aws.AwsError as e:
        lines.append(f"state history unavailable ({str(e)[:120]})")
    return "\n".join(lines)


def _fetch_datapoints(namespace: str, metric: str, stat: str, hours: int,
                      dimensions: Optional[dict], profile: str, region: str) -> list:
    end = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours * 3600))
    args = ["--namespace", namespace, "--metric-name", metric,
            "--start-time", start, "--end-time", end,
            "--period", "300" if hours <= 12 else "900", "--statistics", stat]
    if dimensions:
        args += ["--dimensions"] + [f"Name={k},Value={v}" for k, v in dimensions.items()]
    data = aws.run("cloudwatch", "get-metric-statistics", args, profile=profile, region=region)
    return chart.parse_datapoints(data.get("Datapoints") or [], stat)


def tool_cloudwatch_metric_graph(namespace: str, metric: str, alarm: str = "",
                                 dimensions: Optional[dict] = None, stat: str = "Sum",
                                 hours: int = 6, profile: str = "", region: str = "",
                                 key: str = "", title: str = "") -> str:
    """Render any CloudWatch metric as a PNG and return its evidence key.

    Pass `alarm` instead of profile/region to inherit the account and region the
    alarm actually lives in — that is what keeps a GovCloud alert from being
    graphed against the commercial partition.
    """
    if alarm and not (profile and region):
        try:
            found = aws.find_alarm(alarm)
            profile, region = found["_profile"], found["_region"]
        except aws.AwsError as e:
            return f"LOOKUP FAILED — {e}"
    profile = profile or aws.profiles()[0]
    region = region or aws.regions()[0]
    hours = max(1, min(int(hours), 336))
    dims = [[k, str(v)] for k, v in (dimensions or {}).items()]
    metric_spec = [namespace, metric]
    for name, value in dims:
        metric_spec += [name, value]
    metric_spec.append({"label": metric, "stat": stat})
    widget = {
        "metrics": [metric_spec],
        "period": 300 if hours <= 12 else 900,
        "region": region,
        "title": title or f"{metric} ({stat}) — last {hours}h",
        "width": 1100, "height": 340, "view": "timeSeries",
        "start": f"-PT{hours}H", "end": "P0D",
        "yAxis": {"left": {"min": 0}},
    }
    slug = f"cw_{namespace}_{metric}_{stat}".replace("/", "_").replace("-", "_").lower()
    caption = (f"CloudWatch {namespace} {metric} ({stat}) · last {hours}h · {region}"
               + (f" · {', '.join(f'{k}={v}' for k, v in dims)}" if dims else ""))
    key = key or slug
    answer = _render_widget(widget, key, caption, profile, region)
    if not answer.startswith("RENDER FAILED"):
        return answer

    # CloudWatch's own image API is not usable in every account — in the
    # GovCloud account behind us-1-gov it answers "Throttling: Rate exceeded"
    # every time, including after retries, while the same call in commercial
    # returns a PNG (measured 2026-08-31). The DATA is available there, so draw
    # it locally rather than posting a throttling alert with no graph. The
    # caption says which renderer produced it.
    try:
        points = _fetch_datapoints(namespace, metric, stat, hours, dimensions, profile, region)
    except aws.AwsError as e:
        return f"{answer}\nAnd the datapoints could not be read either: {str(e)[:200]}"
    if not points:
        return (f"{answer}\nNo datapoints in this window either, so there is nothing to draw. "
                f"Report 'no data' — that is a real finding for a throttling alarm.")
    threshold = None
    if alarm:
        try:
            threshold = aws.find_alarm(alarm).get("Threshold")
        except aws.AwsError:
            threshold = None
    local_caption = (f"{caption} — drawn locally from CloudWatch datapoints "
                     f"(CloudWatch's own image API is unavailable in this account)")
    try:
        out_path = EVIDENCE_DIR / f"{key}.png"
        chart.render_series_png(points, title or f"{metric} ({stat}) — {region}", out_path,
                                subtitle=f"{namespace} · last {hours}h · account {profile}",
                                threshold=threshold, unit="")
    except Exception as e:                              # noqa: BLE001
        return f"{answer}\nLocal fallback render also failed: {type(e).__name__}: {e}"
    _record(key, out_path, local_caption)
    return (f"OK — CloudWatch would not render this one, so it was drawn locally from the "
            f"same datapoints ({out_path.stat().st_size} bytes, {len(points)} points).\n"
            f"evidence_key: {key}\ncaption: {local_caption}\n"
            f"Cite \"{key}\" in a post's evidence_keys to attach it. Say the graph was "
            f"rendered from CloudWatch data rather than implying it is a console screenshot.")


def tool_cloudwatch_metric_values(namespace: str, metric: str, alarm: str = "",
                                  dimensions: Optional[dict] = None, stat: str = "Sum",
                                  hours: int = 6, profile: str = "", region: str = "") -> str:
    """The numbers behind a CloudWatch metric: total, peak, latest, and how many
    datapoints exist at all. No datapoints is a real finding, not a zero."""
    if alarm and not (profile and region):
        try:
            found = aws.find_alarm(alarm)
            profile, region = found["_profile"], found["_region"]
        except aws.AwsError as e:
            return f"LOOKUP FAILED — {e}"
    profile = profile or aws.profiles()[0]
    region = region or aws.regions()[0]
    hours = max(1, min(int(hours), 336))
    end = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours * 3600))
    args = ["--namespace", namespace, "--metric-name", metric,
            "--start-time", start, "--end-time", end,
            "--period", "300", "--statistics", stat]
    if dimensions:
        args += ["--dimensions"] + [f"Name={k},Value={v}" for k, v in dimensions.items()]
    try:
        data = aws.run("cloudwatch", "get-metric-statistics", args,
                       profile=profile, region=region)
    except aws.AwsError as e:
        return f"QUERY FAILED — {e}"
    points = sorted(data.get("Datapoints") or [], key=lambda p: p.get("Timestamp", ""))
    header = (f"{namespace} {metric} ({stat}) in {region} [{profile}], {start} -> {end}")
    if not points:
        return (f"{header}\nNO DATAPOINTS at all in this window. That is not a zero reading — "
                f"the metric published nothing. An alarm on it will sit in INSUFFICIENT_DATA; "
                f"report it as no data.")
    values = [p.get(stat, 0) for p in points]
    return "\n".join([
        header,
        f"  datapoints: {len(points)}",
        f"  latest:     {values[-1]:.2f} at {points[-1].get('Timestamp')}",
        f"  peak:       {max(values):.2f}",
        f"  total:      {sum(values):.2f}" if stat == "Sum" else
        f"  average:    {sum(values) / len(values):.2f}",
    ])


def tool_alb_target_health(load_balancer: str) -> str:
    """Which targets behind this load balancer are healthy, and WHY the unhealthy
    ones are not.

    The entry point for ALBUnhealthyHostCritical. The alert's summary carries the
    ARN tail ("app/uk-prod-fastcore-app-http/992c86a8ba9f5fb3"), which is all
    this needs — it searches the configured accounts/regions for that balancer
    and reports which one it found.

    `TargetHealth.Reason` is the field that matters: Target.FailedHealthChecks is
    a real failure, Elb.RegistrationInProgress / Target.HealthCheckInProgress is
    a warm-up that resolves itself, and Target.DeregistrationInProgress is a
    deploy in flight. Those are three different threads.
    """
    try:
        balancer = aws.find_load_balancer(load_balancer)
        account = aws.whoami(balancer["_profile"])
        groups = aws.target_groups_for(balancer)
    except aws.AwsError as e:
        return f"LOOKUP FAILED — {e}"

    lines = [f"load balancer: {balancer.get('LoadBalancerName')}  ({balancer.get('Type')})",
             f"account:       {account.account_id} (profile {balancer['_profile']})",
             f"region:        {balancer['_region']}",
             f"state:         {(balancer.get('State') or {}).get('Code', '?')}",
             f"scheme:        {balancer.get('Scheme')}  azs: "
             + ", ".join(z.get("ZoneName", "?") for z in balancer.get("AvailabilityZones") or []),
             f"cw dimension:  {balancer['_dimension']}"]
    if not groups:
        lines.append("no target groups attached — nothing to check")
        return "\n".join(lines)

    total_bad = 0
    for group in groups:
        try:
            targets = aws.target_health(balancer, group["TargetGroupArn"])
        except aws.AwsError as e:
            lines.append(f"\n{group.get('TargetGroupName')}: health unavailable ({str(e)[:120]})")
            continue
        states: Dict[str, int] = {}
        for target in targets:
            states[(target.get("TargetHealth") or {}).get("State", "?")] = \
                states.get((target.get("TargetHealth") or {}).get("State", "?"), 0) + 1
        healthy, unhealthy = states.get("healthy", 0), states.get("unhealthy", 0)
        total_bad += unhealthy
        lines.append(f"\n{group.get('TargetGroupName')}  "
                     f"[{group.get('Protocol')}:{group.get('Port')}, "
                     f"health check {group.get('HealthCheckPath') or group.get('HealthCheckProtocol')}]")
        lines.append(f"    {healthy} healthy / {len(targets)} registered"
                     + (f", {unhealthy} UNHEALTHY" if unhealthy else "")
                     + (f", other: {states}" if len(states) > 2 else ""))
        lines.append(f"    cw dimension: {group['_dimension']}")
        for target in targets:
            health = target.get("TargetHealth") or {}
            if health.get("State") == "healthy":
                continue
            lines.append(f"    !! {(target.get('Target') or {}).get('Id')}:"
                         f"{(target.get('Target') or {}).get('Port')}  "
                         f"{health.get('State')}  reason={health.get('Reason') or '-'}  "
                         f"{' '.join((health.get('Description') or '').split())[:140]}")
    if not total_bad:
        lines.append("\nEvery registered target is healthy right now. The alert fires on "
                     "'at least 1 unhealthy for at least 15m', so it may have recovered since "
                     "— check the UnHealthyHostCount graph before calling it a false alarm.")
    return "\n".join(lines)


def tool_alb_health_graph(load_balancer: str, target_group: str = "", hours: int = 3,
                          key: str = "") -> str:
    """Render healthy vs unhealthy host counts for this load balancer's target
    group(s) and return the evidence key(s).

    Both series on ONE graph on purpose: they share a unit (a count of hosts) and
    the reader's question is the ratio between them, which is the one case where
    two series belong together.
    """
    try:
        balancer = aws.find_load_balancer(load_balancer)
        groups = aws.target_groups_for(balancer)
    except aws.AwsError as e:
        return f"LOOKUP FAILED — {e}"
    if target_group:
        groups = [g for g in groups
                  if target_group.lower() in (g.get("TargetGroupName") or "").lower()]
    if not groups:
        return (f"No target group matches {target_group!r} on "
                f"{balancer.get('LoadBalancerName')}.")

    hours = max(1, min(int(hours), 336))
    answers = []
    for group in groups[:3]:
        widget = {
            "metrics": [
                ["AWS/ApplicationELB", "HealthyHostCount", "LoadBalancer",
                 balancer["_dimension"], "TargetGroup", group["_dimension"],
                 {"label": "healthy", "stat": "Average", "color": "#2ca02c"}],
                ["AWS/ApplicationELB", "UnHealthyHostCount", "LoadBalancer",
                 balancer["_dimension"], "TargetGroup", group["_dimension"],
                 {"label": "unhealthy", "stat": "Maximum", "color": "#d62728"}],
            ],
            "period": 300 if hours <= 12 else 900,
            "region": balancer["_region"],
            "title": f"{group.get('TargetGroupName')} — healthy vs unhealthy hosts "
                     f"(last {hours}h)",
            "width": 1100, "height": 340, "view": "timeSeries",
            "start": f"-PT{hours}H", "end": "P0D", "yAxis": {"left": {"min": 0}},
        }
        slug = (key or f"alb_{group.get('TargetGroupName')}").replace("-", "_").lower()
        caption = (f"ALB {balancer.get('LoadBalancerName')} / "
                   f"{group.get('TargetGroupName')} — healthy vs unhealthy hosts, last "
                   f"{hours}h, {balancer['_region']}")
        answers.append(_render_widget(widget, slug, caption,
                                      balancer["_profile"], balancer["_region"]))
    return "\n\n".join(answers)


TOOLS = [
    {"name": "alb_target_health",
     "description": "For ALBUnhealthyHostCritical: which targets behind the load balancer are "
                    "healthy, how many are registered, and the REASON each unhealthy one is "
                    "failing (FailedHealthChecks vs RegistrationInProgress vs "
                    "DeregistrationInProgress — three different threads). Pass the name or the "
                    "ARN tail from the alert summary, e.g. "
                    "'app/uk-prod-fastcore-app-http/992c86a8ba9f5fb3'.",
     "inputSchema": {"type": "object", "properties": {"load_balancer": {"type": "string"}},
                     "required": ["load_balancer"]},
     "handler": tool_alb_target_health},
    {"name": "alb_health_graph",
     "description": "Render healthy vs unhealthy host counts for the load balancer's target "
                    "group(s) as one PNG per group, and return the evidence key(s).",
     "inputSchema": {"type": "object", "properties": {
         "load_balancer": {"type": "string"}, "target_group": {"type": "string"},
         "hours": {"type": "integer"}, "key": {"type": "string"}},
         "required": ["load_balancer"]},
     "handler": tool_alb_health_graph},
    {"name": "cloudwatch_alarm",
     "description": "What a CloudWatch alarm watches (namespace, metric, stat, period, "
                    "threshold, dimensions), its current state and its recent state changes. "
                    "START HERE when the alert name IS an alarm name, e.g. "
                    "Rekognition-ThrottledCount-High-wpsc01. Searches every configured "
                    "account/region, including GovCloud, and reports which one it found.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}},
                     "required": ["name"]},
     "handler": tool_cloudwatch_alarm},
    {"name": "cloudwatch_metric_graph",
     "description": "Render ANY CloudWatch metric as a PNG and return its evidence key. Pass "
                    "`alarm` to inherit that alarm's account and region — which is what keeps "
                    "a GovCloud alert from being graphed against the commercial partition.",
     "inputSchema": {"type": "object", "properties": {
         "namespace": {"type": "string"}, "metric": {"type": "string"},
         "alarm": {"type": "string"}, "dimensions": {"type": "object"},
         "stat": {"type": "string"}, "hours": {"type": "integer"},
         "profile": {"type": "string"}, "region": {"type": "string"},
         "key": {"type": "string"}, "title": {"type": "string"}},
         "required": ["namespace", "metric"]},
     "handler": tool_cloudwatch_metric_graph},
    {"name": "cloudwatch_metric_values",
     "description": "The numbers behind any CloudWatch metric — datapoint count, latest, peak, "
                    "total/average. Distinguishes 'no datapoints' from 'zero'.",
     "inputSchema": {"type": "object", "properties": {
         "namespace": {"type": "string"}, "metric": {"type": "string"},
         "alarm": {"type": "string"}, "dimensions": {"type": "object"},
         "stat": {"type": "string"}, "hours": {"type": "integer"},
         "profile": {"type": "string"}, "region": {"type": "string"}},
         "required": ["namespace", "metric"]},
     "handler": tool_cloudwatch_metric_values},
    {"name": "list_rds_instances",
     "description": "Every RDS instance the configured AWS profiles can see, with account, "
                    "region, engine, class and whether Performance Insights is on. Start "
                    "here when the alert names an environment rather than a database.",
     "inputSchema": {"type": "object", "properties": {"name_filter": {"type": "string"}}},
     "handler": tool_list_rds_instances},
    {"name": "rds_instance_summary",
     "description": "Where one database lives (account, region, AZ, Multi-AZ) and what it is "
                    "(engine, class, storage, status), plus whether top-SQL is available.",
     "inputSchema": {"type": "object", "properties": {"instance": {"type": "string"}},
                     "required": ["instance"]},
     "handler": tool_rds_instance_summary},
    {"name": "rds_metrics",
     "description": "Current, average and peak CloudWatch values for an RDS instance "
                    "(CPUUtilization, DatabaseConnections, FreeableMemory, IOPS, queue "
                    "depth). The NUMBERS for the thread text — read these, never a graph.",
     "inputSchema": {"type": "object", "properties": {
         "instance": {"type": "string"}, "hours": {"type": "integer"},
         "metrics": {"type": "array", "items": {"type": "string"}}},
         "required": ["instance"]},
     "handler": tool_rds_metrics},
    {"name": "rds_metric_graph",
     "description": "Render a CloudWatch graph PNG (server-side, no browser) and return its "
                    "evidence key to attach. One metric per graph: CPUUtilization and "
                    "DatabaseConnections have different units.",
     "inputSchema": {"type": "object", "properties": {
         "instance": {"type": "string"},
         "metrics": {"type": "array", "items": {"type": "string"}},
         "hours": {"type": "integer"}, "key": {"type": "string"}, "title": {"type": "string"}},
         "required": ["instance"]},
     "handler": tool_rds_metric_graph},
    {"name": "rds_top_queries",
     "description": "Top SQL by database load and the top wait events, from Performance "
                    "Insights. Statements come back tokenized by AWS ($1 for values).",
     "inputSchema": {"type": "object", "properties": {
         "instance": {"type": "string"}, "hours": {"type": "integer"},
         "limit": {"type": "integer"}}, "required": ["instance"]},
     "handler": tool_rds_top_queries},
    {"name": "rds_events",
     "description": "Recent RDS events for an instance — failover, reboot, storage "
                    "autoscaling, parameter group change. Cheap, and often the answer.",
     "inputSchema": {"type": "object", "properties": {
         "instance": {"type": "string"}, "hours": {"type": "integer"}},
         "required": ["instance"]},
     "handler": tool_rds_events},
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
        except Exception as e:                          # noqa: BLE001
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


def _selftest(instance: str) -> int:
    """Diagnostics to stderr — stdout is protocol."""
    print(f"profiles: {', '.join(aws.profiles())}  regions: "
          f"{', '.join(aws.regions())}", file=sys.stderr)
    for label, fn, kwargs in (
        ("list_rds_instances", tool_list_rds_instances, {}),
        ("rds_instance_summary", tool_rds_instance_summary, {"instance": instance}),
        ("rds_metrics", tool_rds_metrics, {"instance": instance, "hours": 3}),
        ("rds_top_queries", tool_rds_top_queries, {"instance": instance, "hours": 1, "limit": 3}),
        ("rds_events", tool_rds_events, {"instance": instance}),
        ("rds_metric_graph", tool_rds_metric_graph,
         {"instance": instance, "metrics": ["CPUUtilization"], "hours": 3}),
    ):
        print(f"\n=== {label} ===", file=sys.stderr)
        try:
            print(str(fn(**kwargs))[:1400], file=sys.stderr)
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
        rest = [a for a in sys.argv[1:] if a != "--selftest"]
        sys.exit(_selftest(rest[0] if rest else "stage-core-rds2"))
    serve()
