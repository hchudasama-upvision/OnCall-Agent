#!/usr/bin/env python3
"""
"Which process is eating the CPU?" — for a Windows VM, two ways, in order.

The owner's direction (2026-08-31): a VM CPU alarm must name the top processes,
not just show a Grafana line. There are two sources, and the order matters:

1. PROMETHEUS, no credentials at all. 93 Windows hosts run windows_exporter
   with the process collector on, so Thanos already holds
   `windows_process_cpu_time_total` and `windows_process_working_set_bytes` per
   process (218-442 processes per host). Verified live on SVC182 (2026-08-31):
   top CPU `consul` 1.30%, top memory `w3wp` 2701 MB. This is the path that
   should almost always answer, and it needs no login, no open port and no
   secret.

2. WINRM, only when the host has no exporter. STG-Backend5 — the VM in the
   owner's own alert — is exactly that case: it reports no `windows_os_hostname`
   series, so there is no exporter to ask. See winrm_client.py for why that
   surface is deliberately query-shaped rather than command-shaped.

   On STG-Backend5 specifically WinRM authenticates but refuses to create a
   shell (`0x80070002`, every command including `hostname` via cmd), which is a
   server-side WinRM/shell-plugin setting on that VM. So today this path returns
   an explanation, not data — and installing windows_exporter on that host would
   be the better fix anyway, since it also removes the credential entirely.
"""
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from ..Grafana_Agent import thanos
from . import winrm_client

# The idle process is per-core idle time and sums to hundreds of percent — it is
# not a consumer, and leaving it in makes it the top result on every host
# (measured: 593.68% on SVC182). `_Total` is the same problem for the rollup.
_NOT_A_PROCESS = {"idle", "_total", "system idle process"}

CPU_WINDOW = os.environ.get("WINDOWS_PROCESS_WINDOW", "5m")

# A VM name from vCenter is bare ("STG-Backend5") and does not resolve on its
# own — only the FQDN does. windows_exporter lookups want the bare name, WinRM
# wants something resolvable, so the two are kept separate rather than one
# stripped string being used for both (the first version passed the stripped
# name to WinRM and failed on DNS instead of reaching the host).
DNS_SUFFIXES = [s.strip() for s in
                os.environ.get("WINDOWS_DNS_SUFFIXES",
                               "pandostaging.local,verimatch.com").split(",") if s.strip()]


def _resolvable_names(host: str) -> List[str]:
    import socket

    bare = re.sub(r"\.(" + "|".join(re.escape(s) for s in DNS_SUFFIXES) + r")$",
                  "", (host or "").strip(), flags=re.I)
    candidates = [host.strip()] + [f"{bare}.{suffix}" for suffix in DNS_SUFFIXES] + [bare]
    resolvable, seen = [], set()
    for candidate in candidates:
        if not candidate or candidate.lower() in seen:
            continue
        seen.add(candidate.lower())
        try:
            socket.getaddrinfo(candidate, None)
        except OSError:
            continue
        resolvable.append(candidate)
    return resolvable


@dataclass
class ProcessRow:
    name: str
    value: float
    unit: str


@dataclass
class TopProcesses:
    host: str
    source: str                     # "prometheus" | "winrm"
    instance: str = ""
    by_cpu: List[ProcessRow] = field(default_factory=list)
    by_memory: List[ProcessRow] = field(default_factory=list)
    note: str = ""


def resolve_instance(host: str) -> Optional[str]:
    """The windows_exporter instance for a hostname, or None if it has none.

    None is a real answer, not a failure: only 93 of the 177 hosts that appear
    in `windows_os_hostname` run the process collector, and a VM from vCenter
    may not appear there at all.
    """
    name = re.sub(r"\.(pandostaging\.local|verimatch\.com)$", "", (host or "").strip(), flags=re.I)
    if not name:
        return None
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}:\d+$", name):      # already an instance
        return name
    try:
        series = thanos.query(f'windows_os_hostname{{hostname=~"(?i){re.escape(name)}"}}')
    except Exception:                                        # noqa: BLE001
        return None
    for item in series:
        instance = (item.get("metric") or {}).get("instance")
        if instance:
            return instance
    return None


def _rows(expr: str, unit: str, divisor: float = 1.0, limit: int = 5) -> List[ProcessRow]:
    out: List[ProcessRow] = []
    for item in thanos.query(expr):
        name = (item.get("metric") or {}).get("process") or "?"
        if name.strip().lower() in _NOT_A_PROCESS:
            continue
        try:
            value = float(item["value"][1]) / divisor
        except (KeyError, ValueError, TypeError):
            continue
        out.append(ProcessRow(name=name, value=value, unit=unit))
    out.sort(key=lambda r: -r.value)
    return out[:limit]


def top_from_prometheus(instance: str, limit: int = 5) -> Tuple[List[ProcessRow], List[ProcessRow]]:
    """Top processes by CPU rate and by working-set memory, from windows_exporter.

    CPU is a RATE over CPU-seconds, expressed as percent of one core — the raw
    counter is cumulative and would read as a meaningless total.
    """
    by_cpu = _rows(
        f'topk({limit + 3}, rate(windows_process_cpu_time_total{{instance="{instance}"}}'
        f'[{CPU_WINDOW}]) * 100)', "% of one core", limit=limit)
    by_memory = _rows(
        f'topk({limit + 3}, windows_process_working_set_bytes{{instance="{instance}"}})',
        "MB", divisor=1024 * 1024, limit=limit)
    return by_cpu, by_memory


def top_processes(host: str, limit: int = 5) -> TopProcesses:
    """Top processes for a Windows host — Prometheus first, WinRM only if needed."""
    instance = resolve_instance(host)
    if instance:
        try:
            by_cpu, by_memory = top_from_prometheus(instance, limit=limit)
        except Exception as e:                          # noqa: BLE001
            return TopProcesses(host=host, source="prometheus", instance=instance,
                                note=f"windows_exporter instance {instance} found but the query "
                                     f"failed: {str(e).splitlines()[0][:160]}")
        if by_cpu or by_memory:
            return TopProcesses(host=host, source="prometheus", instance=instance,
                                by_cpu=by_cpu, by_memory=by_memory,
                                note=f"from windows_exporter on {instance} — no login used. "
                                     f"CPU is a {CPU_WINDOW} rate as percent of ONE core, so a "
                                     f"multi-core host can exceed 100%.")
        return TopProcesses(host=host, source="prometheus", instance=instance,
                            note=f"{instance} reports windows_exporter but no per-process "
                                 f"series — the process collector is off on that host.")

    # No exporter: try the credentialled path against a name that actually
    # resolves, and be explicit about what it is.
    targets = _resolvable_names(host)
    if not targets:
        return TopProcesses(
            host=host, source="winrm",
            note=(f"{host} runs no windows_exporter AND does not resolve in DNS (tried "
                  f"{', '.join(DNS_SUFFIXES)}), so there is no way to read its processes from "
                  f"here. Report the breakdown as unavailable."))
    failures = []
    for target in targets:
        # WMI Enumerate first: it needs no shell, and the shell is exactly what
        # STG-Backend5 refuses (0x80070002). Falls through to the shell queries
        # where WMI is the thing that is blocked instead.
        try:
            rows = winrm_client.top_processes_via_wmi(target, limit=limit)
            if rows:
                return TopProcesses(
                    host=host, source="winrm-wmi",
                    by_cpu=[ProcessRow(name=r["Name"], value=float(r["CpuPct"] or 0),
                                       unit="% of total CPU") for r in rows],
                    by_memory=sorted(
                        [ProcessRow(name=r["Name"], value=float(r["WorkingSetMB"] or 0),
                                    unit="MB") for r in rows],
                        key=lambda p: -p.value)[:limit],
                    note=(f"from a read-only WMI query on {target} (no exporter on this host, "
                          f"and no shell needed). CPU here is percent of TOTAL CPU as Windows "
                          f"reports it, not per-core."))
        except winrm_client.WinRmError as e:
            failures.append(f"{target} (wmi): {e}")
        try:
            cpu = winrm_client.run_query(target, "top_cpu")
            memory = winrm_client.run_query(target, "top_memory")
        except winrm_client.WinRmError as e:
            failures.append(f"{target} (shell): {e}")
            continue
        break
    else:
        return TopProcesses(
            host=host, source="winrm",
            note=(f"{host} runs no windows_exporter (no `windows_os_hostname` series), so "
                  f"per-process detail can only come from a login — and that failed. "
                  f"{failures[0]}. Report that the process breakdown is unavailable for this "
                  f"host; do not infer which process is busy from the CPU graph alone."))
    return TopProcesses(
        host=host, source="winrm",
        by_cpu=[ProcessRow(name=r.get("Name", "?"),
                           value=float(r.get("CPUSeconds") or 0), unit="CPU seconds")
                for r in cpu.rows][:limit],
        by_memory=[ProcessRow(name=r.get("Name", "?"),
                              value=float(r.get("WorkingSetMB") or 0), unit="MB")
                   for r in memory.rows][:limit],
        note=(f"from a read-only WinRM query on {host} (no exporter on this host). CPU here is "
              f"CUMULATIVE CPU seconds since the process started, not a percentage — say so "
              f"if you quote it."))
