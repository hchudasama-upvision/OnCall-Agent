#!/usr/bin/env python3
"""
Runscope tool for the investigating model — the synthetic-test specialist's
one tool.

Why one tool, not several
--------------------------
grafana_mcp_server.py exposes several tools because Grafana investigation is
genuinely exploratory: which dashboard, which panel, which variable, does the
metric even exist for this host. Runscope isn't — "find the test this alert
means, get its latest run, see what failed" is a fixed lookup chain with no
judgment calls in it, so decomposing it into find_test/get_latest/get_detail
tools would just make the model perform steps that have only one right order
and no exploration value. runscope_client.fetch_current_evidence already IS
that chain, tested against the real API.
"""
import json
import sys
from typing import Any, Optional

from . import runscope_client

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "noc-runscope", "version": "0.1.0"}


def tool_runscope_check(name_query: str) -> str:
    """Find the Runscope test matching this alert's name and report its
    latest run: pass/fail, and exactly which step/assertion failed if any.
    This is real, current, authoritative evidence — stronger than any
    historical thread, since it's what's happening right now."""
    try:
        evidence = runscope_client.fetch_current_evidence(name_query)
    except Exception as e:                              # noqa: BLE001 — let the model adapt
        return f"RUNSCOPE LOOKUP FAILED — {type(e).__name__}: {str(e).splitlines()[0]}"

    if not evidence:
        return (f"No Runscope test found matching {name_query!r}. This alert type may not be "
               f"Runscope-backed, or the test has a different name than the alert — say so "
               f"rather than assuming a result.")

    if not evidence.failing_steps:
        return (f"Test '{evidence.test_name}', run at {evidence.started_at}: overall "
               f"'{evidence.overall_result}', {evidence.assertions_passed} passed / "
               f"{evidence.assertions_failed} failed assertion(s). "
               + ("No specific failing step detail available." if evidence.assertions_failed
                  else "Currently passing — if the alert is still firing, the underlying "
                       "condition may have already cleared, or this test may not be the "
                       "right one for this alert."))

    lines = [f"Test '{evidence.test_name}', run at {evidence.started_at}: overall "
            f"'{evidence.overall_result}', {evidence.assertions_passed} passed / "
            f"{evidence.assertions_failed} failed assertion(s)."]
    for step in evidence.failing_steps:
        lines.append(f"  FAILING STEP: {step.url}")
        for a in step.failed_assertions:
            target = a.get("target_value")
            actual = str(a.get("actual_value", ""))[:300]
            lines.append(f"    assertion ({a.get('comparison')}): expected {target!r}, got {actual!r}")
    lines.append(f"\nThe step URL(s) above are real and safe to cite verbatim if relevant.")
    return "\n".join(lines)


TOOLS = [
    {"name": "runscope_check",
     "description": "Find the Runscope synthetic test matching this alert and report its "
                    "current/latest run — real, current evidence, not history. Use the alert's "
                    "own name/title as name_query.",
     "inputSchema": {"type": "object", "properties": {
         "name_query": {"type": "string"}}, "required": ["name_query"]},
     "handler": tool_runscope_check},
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


def _selftest() -> int:
    for name, args in (("runscope_check", {"name_query": "Site DNS Valdation"}),):
        try:
            print(f"\n=== {name} ===\n{_HANDLERS[name](**args)[:500]}", file=sys.stderr)
        except Exception as e:                          # noqa: BLE001
            print(f"\n=== {name} FAILED === {type(e).__name__}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    serve()
