#!/usr/bin/env python3
"""
Read-only Jira tools for the investigating model — lets a specialist check
whether an EXISTING ticket already tracks or explains this exact issue
(vendor-side bugs, known flaky tests, ongoing infra work) instead of only
ever citing a case-library note frozen at whenever it was written. See
jira_client.py for the REST client and the read-only/credential notes.

Same protocol shape as every other MCP server in this repo (slack/grafana/
edge_ui/runscope/github): line-delimited JSON-RPC over stdio, stdout carries
protocol only, every diagnostic goes to stderr.
"""
import json
import sys
from typing import Any, Optional

from . import jira_client

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "noc-jira", "version": "0.1.0"}


def tool_search_issues(query: str, limit: int = 10) -> str:
    try:
        issues = jira_client.search_issues(query, limit)
    except Exception as e:                              # noqa: BLE001 — let the model adapt
        return f"SEARCH FAILED — {e}"
    if not issues:
        return f"(no Jira issues matched {query!r})"
    return "\n".join(f"{i.key}  [{i.status}] {i.summary}\n  updated={i.updated}\n  {i.html_url}"
                     for i in issues)


def tool_get_issue(key: str) -> str:
    try:
        i = jira_client.get_issue(key)
    except Exception as e:                              # noqa: BLE001
        return f"FETCH FAILED — {e}"
    return (f"{i.key}  [{i.status}] {i.summary}\n"
           f"assignee={i.assignee} reporter={i.reporter}\n"
           f"created={i.created} updated={i.updated} comments={i.comment_count}\n"
           f"{i.html_url}\n\ndescription:\n{i.description or '(none)'}")


def tool_list_comments(key: str, limit: int = 5) -> str:
    try:
        comments = jira_client.list_comments(key, limit)
    except Exception as e:                              # noqa: BLE001
        return f"FETCH FAILED — {e}"
    if not comments:
        return f"(no comments on {key})"
    return "\n\n".join(f"{c.author}  {c.created}\n{c.body or '(empty)'}" for c in comments)


TOOLS = [
    {"name": "search_issues",
     "description": "Full-text search Jira (summary + description + comments) for a literal phrase "
                    "— an alert name, an error code, a hostname/cluster — to find an EXISTING ticket "
                    "that already tracks or explains this issue. Newest-updated first.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "limit": {"type": "integer"}},
         "required": ["query"]},
     "handler": tool_search_issues},
    {"name": "get_issue",
     "description": "Full detail of one Jira issue by key (e.g. 'NOC-13707') — real current "
                    "status/assignee/description, not a case-library note's memory of it.",
     "inputSchema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
     "handler": tool_get_issue},
    {"name": "list_comments",
     "description": "Most recent comments on a Jira issue, newest first — often where the actual "
                    "root cause / resolution / handoff is written, not the description.",
     "inputSchema": {"type": "object", "properties": {
         "key": {"type": "string"}, "limit": {"type": "integer"}},
         "required": ["key"]},
     "handler": tool_list_comments},
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
        except Exception as e:                          # noqa: BLE001 — let the model adapt
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
    for name, args in (
        ("search_issues", {"query": "Runscope"}),
    ):
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
