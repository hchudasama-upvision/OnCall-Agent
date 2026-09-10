#!/usr/bin/env python3
"""
Read-only WinRM access to Windows VMs — top processes, memory, disk.

Why this exists
---------------
The owner's direction (2026-08-31): a VM CPU alarm should not stop at a Grafana
picture. It should say WHICH PROCESS is burning the CPU, and carry disk and
memory alongside it.

For 93 of the Windows hosts that needs no login at all: they run
windows_exporter and Thanos already holds `windows_process_cpu_time_total` per
process (218-442 processes each), so `Agents/Grafana_Agent` can answer top-N
from PromQL. Check that FIRST — see windows_processes.top_from_prometheus().

STG-Backend5, the VM in the owner's alert, is not one of them: it reports no
`windows_os_hostname` series at all, so there is no exporter and no per-process
metric. That is the only reason this module exists.

The single most important property
----------------------------------
WinRM can run anything. So this module does NOT take a command. It exposes a
fixed set of NAMED read-only queries (_QUERIES), each a literal PowerShell
string in this file, and the caller may only pick one by name. There is no
passthrough parameter, no string interpolation into a command, and no way to
reach `Stop-Process`, `Restart-Computer`, `Set-*`, `Remove-*` or a shell. The
tool surface is data-shaped, not command-shaped — the same discipline as the
fixed kubectl verbs and the allow-listed aws subcommands, applied to the one
surface where getting it wrong would be worst.

Credentials
-----------
WINDOWS_USERNAME / WINDOWS_PASSWORD from `.env`, read here and passed straight
to the transport. They never enter a prompt, never appear in a command line
(which would expose them in the target's own process list), and are never
logged or returned in tool output. `.env` is gitignored.

These are a NAMED PERSON's domain credentials, inherited the same way
EDGE_USERNAME was, and CLAUDE.md's Phase 0 note applies with more force here:
a service account with read-only rights (Performance Monitor Users / remote
WMI) is the right end state, because this credential can log into every VM in
the domain, and the agent only ever needs four counters.
"""
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

# Fixed, read-only PowerShell. Each is a complete literal — nothing is
# interpolated into these strings, and the caller picks one by NAME.
_QUERIES: Dict[str, str] = {
    # Top processes by CPU seconds. Get-Process' CPU is cumulative CPU time, so
    # it is reported as-is and labelled that way rather than being passed off as
    # "% CPU now", which it is not.
    "top_cpu": (
        "Get-Process | Sort-Object CPU -Descending | Select-Object -First 5 "
        "Name, Id, @{N='CPUSeconds';E={[math]::Round($_.CPU,1)}}, "
        "@{N='WorkingSetMB';E={[math]::Round($_.WorkingSet64/1MB,0)}} | "
        "ConvertTo-Csv -NoTypeInformation"
    ),
    # Working-set memory, which is the question a memory alarm actually asks.
    "top_memory": (
        "Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 5 "
        "Name, Id, @{N='WorkingSetMB';E={[math]::Round($_.WorkingSet64/1MB,0)}}, "
        "@{N='CPUSeconds';E={[math]::Round($_.CPU,1)}} | "
        "ConvertTo-Csv -NoTypeInformation"
    ),
    "memory_totals": (
        "Get-CimInstance Win32_OperatingSystem | Select-Object "
        "@{N='TotalMB';E={[math]::Round($_.TotalVisibleMemorySize/1KB,0)}}, "
        "@{N='FreeMB';E={[math]::Round($_.FreePhysicalMemory/1KB,0)}}, "
        "@{N='UsedPct';E={[math]::Round(100-($_.FreePhysicalMemory/$_.TotalVisibleMemorySize*100),1)}} | "
        "ConvertTo-Csv -NoTypeInformation"
    ),
    "disk_usage": (
        "Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | Select-Object "
        "DeviceID, @{N='SizeGB';E={[math]::Round($_.Size/1GB,1)}}, "
        "@{N='FreeGB';E={[math]::Round($_.FreeSpace/1GB,1)}}, "
        "@{N='UsedPct';E={[math]::Round(100-($_.FreeSpace/$_.Size*100),1)}} | "
        "ConvertTo-Csv -NoTypeInformation"
    ),
    "cpu_now": (
        "Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage "
        "-Average | Select-Object @{N='CpuPct';E={[math]::Round($_.Average,1)}} | "
        "ConvertTo-Csv -NoTypeInformation"
    ),
    "uptime": (
        "Get-CimInstance Win32_OperatingSystem | Select-Object "
        "@{N='LastBoot';E={$_.LastBootUpTime.ToString('u')}}, "
        "@{N='UptimeHours';E={[math]::Round(((Get-Date)-$_.LastBootUpTime).TotalHours,1)}} | "
        "ConvertTo-Csv -NoTypeInformation"
    ),
}

# Hostnames/IPs only — nothing that could become a flag or a second command.
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,253}$")

TIMEOUT = int(os.environ.get("WINRM_TIMEOUT", "40"))
PORT = int(os.environ.get("WINRM_PORT", "5985"))
TRANSPORT = os.environ.get("WINRM_TRANSPORT", "ntlm")


class WinRmError(RuntimeError):
    """A failed WinRM call, surfaced as text. Never contains the password."""


@dataclass
class QueryResult:
    host: str
    query: str
    rows: List[Dict[str, str]]
    raw: str = ""


def available_queries() -> List[str]:
    return sorted(_QUERIES)


def _credentials() -> tuple:
    user = os.environ.get("WINDOWS_USERNAME", "")
    password = os.environ.get("WINDOWS_PASSWORD", "")
    if not user or not password:
        raise WinRmError(
            "WINDOWS_USERNAME/WINDOWS_PASSWORD are not set in .env — no Windows login is "
            "configured, so per-process detail is unavailable for hosts without "
            "windows_exporter. Say that rather than guessing which process is busy.")
    return user, password


def _parse_csv(text: str) -> List[Dict[str, str]]:
    """PowerShell's ConvertTo-Csv output -> rows. Quoted, CRLF, header first."""
    import csv
    import io

    lines = [line for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return []
    return [dict(row) for row in csv.DictReader(io.StringIO("\n".join(lines)))]


def run_query(host: str, query: str) -> QueryResult:
    """Run ONE named read-only query on a Windows host.

    `query` must be a key of _QUERIES. Anything else is refused — this is the
    lock that keeps a remote-execution surface read-only.
    """
    if not _HOST_RE.match(host or ""):
        raise WinRmError(f"Refusing host={host!r}: not a valid hostname or address.")
    if query not in _QUERIES:
        raise WinRmError(
            f"Refusing query={query!r}: this surface runs only its own fixed read-only "
            f"queries ({', '.join(available_queries())}). It cannot run arbitrary commands, "
            f"by design.")
    user, password = _credentials()
    try:
        import winrm
    except ImportError as e:                            # noqa: BLE001
        raise WinRmError("pywinrm is not installed — `pip install pywinrm requests_ntlm`") from e

    session = winrm.Session(
        f"http://{host}:{PORT}/wsman",
        auth=(user, password), transport=TRANSPORT,
        operation_timeout_sec=TIMEOUT, read_timeout_sec=TIMEOUT + 10,
    )
    try:
        response = session.run_ps(_QUERIES[query])
    except Exception as e:                              # noqa: BLE001
        # Never let the exception text carry the credential: pywinrm puts the
        # endpoint (not the password) in messages, but the auth tuple can appear
        # in some tracebacks, so only the first line is surfaced.
        detail = str(e).splitlines()[0][:220]
        if password in detail:
            detail = detail.replace(password, "<redacted>")
        raise WinRmError(f"WinRM to {host}:{PORT} failed ({TRANSPORT}): {detail}") from None
    if response.status_code != 0:
        error = (response.std_err or b"").decode("utf-8", "replace")
        error = " ".join(error.split())[:300]
        raise WinRmError(f"{query} on {host} exited {response.status_code}: {error}")
    text = (response.std_out or b"").decode("utf-8", "replace")
    return QueryResult(host=host, query=query, rows=_parse_csv(text), raw=text.strip())


# ---------------------------------------------------------------- WMI, no shell
#
# Windows has plenty of `ps` equivalents — Get-Process, tasklist, wmic process,
# Get-Counter '\Process(*)\% Processor Time'. The problem on STG-Backend5 was
# never the command: WinRM authenticated and then refused Shell/Create with
# 0x80070002, which is the cmd/powershell plugin being disabled. Every command
# fails identically, including `hostname`.
#
# WS-Management has a second operation that does NOT create a shell:
# Enumerate against a WMI class, handled by a different plugin. Where the shell
# is disabled but WMI is not, this returns per-process CPU and working set from
# Win32_PerfFormattedData_PerfProc_Process — the actual `ps` answer.
#
# STATUS: UNVERIFIED against a live host. It was written after the shell failure
# and could not be tested — STG-Backend5 became unreachable ("no route to host",
# having answered minutes earlier) and 10.60.4.182 resets connections on 5985.
# The code path reports which source answered, so if this is wrong it fails
# visibly rather than silently. Verify it the first time a no-exporter host is
# reachable, and delete this note then.
_WMI_URI = "http://schemas.microsoft.com/wbem/wsman/1/wmi/root/cimv2/*"
_PROCESS_WQL = ("SELECT Name,PercentProcessorTime,WorkingSet FROM "
                "Win32_PerfFormattedData_PerfProc_Process")


def _enumerate_envelope(host: str, wql: str) -> str:
    import uuid

    return f"""<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
 xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:w="http://schemas.dmtf.org/wbem/wsman/1/wsman.xsd"
 xmlns:n="http://schemas.xmlsoap.org/ws/2004/09/enumeration">
 <s:Header>
  <a:To>http://{host}:{PORT}/wsman</a:To>
  <w:ResourceURI s:mustUnderstand="true">{_WMI_URI}</w:ResourceURI>
  <a:ReplyTo><a:Address s:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</a:Address></a:ReplyTo>
  <a:Action s:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2004/09/enumeration/Enumerate</a:Action>
  <w:MaxEnvelopeSize s:mustUnderstand="true">512000</w:MaxEnvelopeSize>
  <a:MessageID>uuid:{uuid.uuid4()}</a:MessageID>
  <w:OperationTimeout>PT{TIMEOUT}S</w:OperationTimeout>
 </s:Header>
 <s:Body><n:Enumerate><w:OptimizeEnumeration/><w:MaxElements>80</w:MaxElements>
  <w:Filter Dialect="http://schemas.microsoft.com/wbem/wsman/1/WQL">{wql}</w:Filter>
 </n:Enumerate></s:Body>
</s:Envelope>"""


def top_processes_via_wmi(host: str, limit: int = 5) -> List[Dict[str, str]]:
    """Per-process CPU and memory over WS-Man Enumerate — no shell required.

    WQL is a fixed literal here (_PROCESS_WQL); nothing is interpolated, so this
    stays as read-only and as narrow as the named-query surface above.
    """
    if not _HOST_RE.match(host or ""):
        raise WinRmError(f"Refusing host={host!r}: not a valid hostname or address.")
    user, password = _credentials()
    try:
        import requests
        from requests_ntlm import HttpNtlmAuth
    except ImportError as e:                            # noqa: BLE001
        raise WinRmError("requests/requests_ntlm are not installed") from e

    try:
        response = requests.post(
            f"http://{host}:{PORT}/wsman",
            data=_enumerate_envelope(host, _PROCESS_WQL).encode(),
            headers={"Content-Type": "application/soap+xml;charset=UTF-8"},
            auth=HttpNtlmAuth(user, password), timeout=TIMEOUT)
    except Exception as e:                              # noqa: BLE001
        detail = str(e).splitlines()[0][:200].replace(password, "<redacted>")
        raise WinRmError(f"WMI enumerate to {host}:{PORT} failed: {detail}") from None
    if response.status_code != 200:
        fault = re.search(r"<f:Message[^>]*>(.*?)</f:Message>", response.text, re.S) or \
            re.search(r"<s:Text[^>]*>(.*?)</s:Text>", response.text, re.S)
        message = " ".join((fault.group(1) if fault else response.text[:300]).split())[:220]
        raise WinRmError(f"WMI enumerate on {host} -> HTTP {response.status_code}: {message}")

    rows: List[Dict[str, str]] = []
    for block in re.findall(r"<p:Win32_PerfFormattedData_PerfProc_Process[^>]*>(.*?)"
                            r"</p:Win32_PerfFormattedData_PerfProc_Process>",
                            response.text, re.S):
        name = re.search(r"<p:Name>(.*?)</p:Name>", block)
        cpu = re.search(r"<p:PercentProcessorTime>(.*?)</p:PercentProcessorTime>", block)
        memory = re.search(r"<p:WorkingSet>(.*?)</p:WorkingSet>", block)
        if not name or name.group(1).strip().lower() in ("idle", "_total"):
            continue
        rows.append({"Name": name.group(1),
                     "CpuPct": (cpu.group(1) if cpu else "0"),
                     "WorkingSetMB": str(int(int(memory.group(1)) / 1048576)) if memory else "0"})
    rows.sort(key=lambda r: -int(r["CpuPct"] or 0))
    return rows[:limit]
