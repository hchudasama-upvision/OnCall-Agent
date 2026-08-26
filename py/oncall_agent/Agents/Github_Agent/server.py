#!/usr/bin/env python3
"""
Read-only GitHub tools for the investigating model — lets a specialist check
real code/PR/deploy state instead of trusting a case-library note frozen at
whenever that entry was written. See github_client.py for the `gh` CLI
wrapper and the `--allowed-tools`/credential notes.

Same protocol shape as every other MCP server in this repo (slack/grafana/
edge_ui/runscope): line-delimited JSON-RPC over stdio, stdout carries
protocol only, every diagnostic goes to stderr.
"""
import json
import sys
from typing import Any, Optional

from . import github_client

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "noc-github", "version": "0.1.0"}


def tool_search_code(query: str, repo: str = "") -> str:
    try:
        matches = github_client.search_code(query, repo)
    except Exception as e:                              # noqa: BLE001 — let the model adapt
        return f"SEARCH FAILED — {e}"
    if not matches:
        return f"(no code matches for {query!r}" + (f" in {repo}" if repo else "") + ")"
    return "\n".join(f"{m.repo}  {m.path}\n  {m.html_url}" for m in matches[:20])


def tool_get_file(repo: str, path: str, ref: str = "") -> str:
    try:
        return github_client.get_file(repo, path, ref)
    except Exception as e:                              # noqa: BLE001
        return f"FETCH FAILED — {e}"


def tool_get_pr(repo: str, number: int) -> str:
    try:
        pr = github_client.get_pr(repo, number)
    except Exception as e:                              # noqa: BLE001
        return f"FETCH FAILED — {e}"
    return (f"PR #{pr.number} {pr.title!r}\nstate={pr.state} merged={pr.merged} "
           f"merged_at={pr.merged_at or '(not merged)'}\n"
           f"merge_commit={pr.merge_commit_sha or '(none)'} base={pr.base_ref} head={pr.head_ref}\n"
           f"{pr.html_url}")


def tool_get_commit(repo: str, sha: str) -> str:
    try:
        c = github_client.get_commit(repo, sha)
    except Exception as e:                              # noqa: BLE001
        return f"FETCH FAILED — {e}"
    files = ", ".join(c.files_changed) or "(none listed)"
    return (f"commit {c.sha}\nauthor={c.author} date={c.date}\n"
           f"message: {c.message}\nfiles: {files}\n{c.html_url}")


def tool_list_workflow_runs(repo: str, workflow: str = "", branch: str = "", limit: int = 10) -> str:
    try:
        runs = github_client.list_workflow_runs(repo, workflow, branch, limit)
    except Exception as e:                              # noqa: BLE001
        return f"FETCH FAILED — {e}"
    if not runs:
        return f"(no workflow runs found for {repo}" + (f" workflow={workflow!r}" if workflow else "") + ")"
    lines = [f"{r.created_at}  {r.name}  status={r.status} conclusion={r.conclusion or '(pending)'} "
            f"branch={r.head_branch}\n  {r.html_url}" for r in runs]
    return "\n".join(lines)


TOOLS = [
    {"name": "search_code",
     "description": "Search code across GitHub (or one repo, e.g. 'veritone/realtime') for a literal "
                    "string — an error code, a function name — to find where it actually lives.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "repo": {"type": "string", "description": "owner/name, optional"}},
         "required": ["query"]},
     "handler": tool_search_code},
    {"name": "get_file",
     "description": "Raw contents of one file at repo/path/ref (default branch if ref is empty). "
                    "Truncated if huge — for reading a specific function/error path, not a whole "
                    "generated file.",
     "inputSchema": {"type": "object", "properties": {
         "repo": {"type": "string"}, "path": {"type": "string"}, "ref": {"type": "string"}},
         "required": ["repo", "path"]},
     "handler": tool_get_file},
    {"name": "get_pr",
     "description": "A pull request's real merge state — was it actually merged, into what, when, "
                    "as which commit. Stronger than a case-library fix_reference alone.",
     "inputSchema": {"type": "object", "properties": {
         "repo": {"type": "string"}, "number": {"type": "integer"}},
         "required": ["repo", "number"]},
     "handler": tool_get_pr},
    {"name": "get_commit",
     "description": "One commit's message, author, date, and changed files.",
     "inputSchema": {"type": "object", "properties": {
         "repo": {"type": "string"}, "sha": {"type": "string"}},
         "required": ["repo", "sha"]},
     "handler": tool_get_commit},
    {"name": "list_workflow_runs",
     "description": "Recent GitHub Actions runs for a repo — status/conclusion, not logs. Use to "
                    "check whether an automated check (e.g. a deploy-verification workflow a "
                    "case-library entry mentions) actually ran and what it concluded.",
     "inputSchema": {"type": "object", "properties": {
         "repo": {"type": "string"}, "workflow": {"type": "string", "description": "name substring filter"},
         "branch": {"type": "string"}, "limit": {"type": "integer"}},
         "required": ["repo"]},
     "handler": tool_list_workflow_runs},
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
        ("search_code", {"query": "ETGN013"}),
        ("get_pr", {"repo": "veritone/realtime", "number": 10693}),
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
