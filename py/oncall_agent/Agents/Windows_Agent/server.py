#!/usr/bin/env python3
"""
Windows VM tools — top processes, disk and memory for a named host.

Given to the Grafana/metrics specialist, which is where the VMware VM alarms
land. Two sources behind one tool (see windows_processes.py): Prometheus'
per-process metrics where the host runs windows_exporter (93 hosts, no
credentials), and a fixed set of read-only WinRM queries where it does not.

Nothing here can change anything on a VM. The WinRM surface takes a query NAME
from a fixed list, never a command (winrm_client.py explains why), and this
server exposes no write tool of any kind.
"""
import json
import sys
from typing import Any, Optional

from . import windows_processes, winrm_client

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "noc-windows", "version": "0.1.0"}


def tool_top_processes(host: str, limit: int = 5) -> str:
    """The top processes by CPU and by memory on a Windows host.

    `host` is the VM/host name from the alert (STG-Backend5, SVC182) or an
    exporter instance (10.60.4.182:9182). Prometheus is tried first and needs no
    login; WinRM is only used for hosts with no exporter.
    """
    try:
        result = windows_processes.top_processes(host, limit=max(1, min(int(limit), 10)))
    except Exception as e:                              # noqa: BLE001
        return f"LOOKUP FAILED — {type(e).__name__}: {str(e).splitlines()[0]}"

    lines = [f"host: {result.host}"
             + (f"  (windows_exporter instance {result.instance})" if result.instance else ""),
             f"source: {result.source}"]
    if result.by_cpu:
        lines.append("top by CPU:")
        lines += [f"    {row.value:10.2f} {row.unit:16} {row.name}" for row in result.by_cpu]
    if result.by_memory:
        lines.append("top by memory:")
        lines += [f"    {row.value:10.0f} {row.unit:16} {row.name}" for row in result.by_memory]
    if result.note:
        lines.append(f"note: {result.note}")
    if not (result.by_cpu or result.by_memory):
        lines.append("No per-process data was obtained. Do not name a process as the cause.")
    return "\n".join(lines)


def tool_windows_host_usage(host: str) -> str:
    """Disk and memory totals from the host itself, for a host with no exporter.

    Only reachable over WinRM, so it is unavailable for hosts where the remote
    shell is disabled — in which case the vCenter disk/memory panels are the
    evidence instead.
    """
    out = [f"host: {host}"]
    for query, label in (("memory_totals", "memory"), ("disk_usage", "disks"),
                         ("cpu_now", "cpu"), ("uptime", "uptime")):
        try:
            result = winrm_client.run_query(host, query)
        except winrm_client.WinRmError as e:
            out.append(f"{label}: unavailable — {e}")
            continue
        for row in result.rows:
            out.append(f"{label}: " + "  ".join(f"{k}={v}" for k, v in row.items()))
    out.append("Read-only WinRM queries only. If these are unavailable, use the vCenter "
               "memory and disk panels and say the in-guest view could not be read.")
    return "\n".join(out)


TOOLS = [
    {"name": "top_processes",
     "description": "Top processes by CPU and by memory on a Windows host — the 'which process "
                    "is eating it' half of a VM CPU/memory alarm. Uses windows_exporter metrics "
                    "when the host has them (no login), a read-only WinRM query otherwise. Pass "
                    "the VM/host name from the alert.",
     "inputSchema": {"type": "object", "properties": {
         "host": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["host"]},
     "handler": tool_top_processes},
    {"name": "windows_host_usage",
     "description": "In-guest disk, memory, CPU and uptime for a Windows host, via read-only "
                    "WinRM. Use only when the host has no windows_exporter; otherwise prefer "
                    "the metrics and the dashboard panels.",
     "inputSchema": {"type": "object", "properties": {"host": {"type": "string"}},
                     "required": ["host"]},
     "handler": tool_windows_host_usage},
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


def _selftest(host: str) -> int:
    print(f"=== top_processes {host} ===\n{tool_top_processes(host)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    if "--selftest" in sys.argv:
        rest = [a for a in sys.argv[1:] if a != "--selftest"]
        sys.exit(_selftest(rest[0] if rest else "SVC182"))
    serve()
