#!/usr/bin/env python3
"""
Edge UI/Controller tools for the investigating model — the engine-failure
specialist's toolset.

Why this exists
----------------
Before this, engine-failure alerts were handled by a fixed Python sequence
(engine_failure_pipeline.py): always the same two screenshots, always the
"most tasks failed" engine when the incident text didn't name one. That
heuristic is a guess a specialist wouldn't need to make — a human on-call
engineer looks at ALL the engines' stats and picks the one that actually
matches the incident, checks a task's detail before assuming the summary
tells the whole story, and only pulls logs once they know which task is
representative. This server exposes those same underlying calls
(edge_api.py / engine_task_stats.py / screenshot.py — none of that logic is
rewritten here, only exposed as tools) so the model can run that same
judgment loop itself.

Statelessness split
--------------------
fetch_engine_task_stats / list_tasks_by_status / get_task_detail /
get_organization_name are plain JSON API calls (requests, no browser) — each
tool call is independent, nothing to hold open between them.

capture_tasks_page_screenshot / capture_engine_page_screenshot /
download_task_and_job_logs need an already-logged-in Playwright page
(screenshot.py's with_edge_ui_session does a real form login + redirect
wait, several seconds — re-logging in on every tool call would be both slow
and wrong). This server holds one Playwright session open per environment,
lazily created on first use and reused for the rest of the investigation:
the contextmanager object is entered manually (`cm.__enter__()`) instead of
via `with`, kept in a module-level dict, and torn down via `atexit` plus a
`finally` around the stdio loop so chromium always closes when this
short-lived subprocess exits.

Read-only apart from evidence PNGs/zips written under EVIDENCE_DIR (same
pattern grafana_mcp_server.py already uses) — nothing here can act on an
engine, restart anything, or post to Slack.
"""
import atexit
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.sync_api import Page

from . import edge_api, edge_environments, engine_task_stats, screenshot

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "noc-edge-ui", "version": "0.1.0"}

EVIDENCE_DIR = Path(os.environ.get("EVIDENCE_DIR")
                    or Path(__file__).resolve().parents[4] / "dist" / "evidence")
MANIFEST = "edge-ui-evidence.json"

# One Playwright session per environment, held open for the life of this
# server process (one investigation) and reused across tool calls.
_sessions: Dict[str, Any] = {}
_pages: Dict[str, Page] = {}


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


def load_manifest(evidence_dir: Optional[Path] = None) -> Dict[str, dict]:
    path = (evidence_dir or EVIDENCE_DIR) / MANIFEST
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except ValueError:
        return {}


def clear_manifest(evidence_dir: Optional[Path] = None) -> None:
    """Start each investigation with an empty manifest — same reasoning as
    grafana_mcp_server.clear_manifest: a screenshot captured for a previous
    alert must not stay citable by this one."""
    path = (evidence_dir or EVIDENCE_DIR) / MANIFEST
    if path.exists():
        path.unlink()


def _env(env_key: str) -> edge_environments.EdgeEnvironment:
    environments = edge_environments.load_edge_environments()
    env = environments.get(env_key)
    if not env:
        raise RuntimeError(
            f"No Edge UI environment configured for {env_key!r}. Configured: {sorted(environments)}"
        )
    return env


def _get_page(env_key: str) -> Page:
    """Lazily log in once per environment, reuse the same page after that."""
    if env_key in _pages:
        return _pages[env_key]
    env = _env(env_key)
    username = os.environ.get("EDGE_USERNAME")
    password = os.environ.get("EDGE_PASSWORD")
    if not username or not password:
        raise RuntimeError("EDGE_USERNAME/EDGE_PASSWORD not set — required for Edge UI screenshots/logs")
    ui_base_url = edge_environments.to_ui_base_url(env)
    cm = screenshot.with_edge_ui_session(ui_base_url, username, password)
    page = cm.__enter__()
    _sessions[env_key] = cm
    _pages[env_key] = page
    return page


def _close_all_sessions() -> None:
    for env_key, cm in list(_sessions.items()):
        try:
            cm.__exit__(None, None, None)
        except Exception:                               # noqa: BLE001 — best-effort cleanup
            pass
    _sessions.clear()
    _pages.clear()


atexit.register(_close_all_sessions)


# --------------------------------------------------------------------- tools

def tool_fetch_engine_task_stats(env_key: str, start_time_epoch_seconds: int,
                                 end_time_epoch_seconds: int) -> str:
    """Per-engine task counts for a window — the raw numbers behind the alert."""
    env = _env(env_key)
    stats = engine_task_stats.fetch_engine_task_stats(env, start_time_epoch_seconds, end_time_epoch_seconds)
    if not stats:
        return f"(no task activity in {env_key} for that window)"
    lines = []
    for e in stats:
        total = engine_task_stats.active_task_total(e)
        counts = " ".join(f"{k}={v}" for k, v in sorted(e.counts.items()) if v)
        lines.append(f"{e.engine_name}  (id {e.engine_id})  active_total={total}  {counts}")
    return "\n".join(lines)


def tool_list_engines_with_failures(env_key: str, start_time_epoch_seconds: int,
                                    end_time_epoch_seconds: int) -> str:
    """Only engines with failed>0 for this window, sorted by failed count
    descending. Use this instead of guessing which engine the incident means
    — especially when the incident text doesn't name one (common: 'Engine
    failure rate 100% for all engine in every Environment' is almost never
    literally every engine)."""
    env = _env(env_key)
    stats = engine_task_stats.fetch_engine_task_stats(env, start_time_epoch_seconds, end_time_epoch_seconds)
    failing = [e for e in stats if e.counts.get("failed", 0) > 0]
    if not failing:
        return f"(no engine in {env_key} has any failed tasks in that window)"
    failing.sort(key=lambda e: e.counts.get("failed", 0), reverse=True)
    lines = []
    for e in failing:
        total = engine_task_stats.active_task_total(e)
        pct = round((e.counts.get("failed", 0) / total) * 100) if total else 0
        lines.append(f"{e.engine_name}  (id {e.engine_id})  failed={e.counts.get('failed', 0)} "
                     f"({pct}%)  total_active={total}")
    return "\n".join(lines)


def tool_list_tasks_by_status(env_key: str, start_time_epoch_seconds: int,
                              end_time_epoch_seconds: int, status: str, limit: int = 100) -> str:
    """Task records matching a status (e.g. 'failed') in a window, newest first."""
    env = _env(env_key)
    records = edge_api.fetch_tasks_by_status(env, start_time_epoch_seconds, end_time_epoch_seconds,
                                             status=status, limit=limit)
    if not records:
        return f"(no {status} tasks in {env_key} for that window)"
    records.sort(key=lambda r: r.modified_date_time, reverse=True)
    lines = [f"{len(records)} {status} task(s):"]
    for r in records[:limit]:
        lines.append(f"  task={r.internal_task_id}  job={r.internal_job_id}  engine={r.engine_name} "
                     f"({r.engine_id})  org={r.internal_organization_id}  modified={r.modified_date_time}")
    return "\n".join(lines)


def tool_get_task_detail(env_key: str, task_id: str) -> str:
    """Full detail for one task: failure_reason, failure_detail (the raw
    error), job id, org id. Pull this for a representative failed task before
    concluding what the error actually is — the summary counts alone don't
    show the error text."""
    env = _env(env_key)
    d = edge_api.fetch_task_detail(env, task_id)
    return (f"task={d.internal_task_id}\njob={d.internal_job_id}\norg={d.internal_organization_id}\n"
           f"engine={d.engine_name} ({d.engine_id})\nfailure_reason={d.failure_reason}\n"
           f"failure_detail={d.failure_detail}\ncreated={d.created_date_time}\n"
           f"modified={d.modified_date_time}")


def tool_get_organization_name(env_key: str, organization_id: str) -> str:
    env = _env(env_key)
    name = edge_api.fetch_organization_name(env, organization_id)
    return name or f"(org {organization_id} has no name populated)"


def tool_list_environments() -> str:
    environments = edge_environments.load_edge_environments()
    return "\n".join(sorted(environments)) or "(none configured)"


def tool_capture_tasks_page_screenshot(env_key: str, engine_name: str, window_minutes: int,
                                       key: str = "") -> str:
    """Screenshot of the Edge UI Tasks page, filtered to one engine + window —
    the visual evidence a human would attach. Requires a real Edge UI login,
    which happens automatically (and only once per environment) on first
    use."""
    return _capture(env_key, engine_name, window_minutes, "/processing/tasks/",
                    key or "edge_ui_tasks_page")


def tool_capture_engine_page_screenshot(env_key: str, engine_name: str, window_minutes: int,
                                        key: str = "") -> str:
    """Screenshot of the Edge UI Engine page, filtered to one engine + window."""
    return _capture(env_key, engine_name, window_minutes, "/processing/engine/",
                    key or "edge_ui_engine_page")


def _capture(env_key: str, engine_name: str, window_minutes: int, path: str, key: str) -> str:
    env = _env(env_key)
    ui_base_url = edge_environments.to_ui_base_url(env)
    try:
        page = _get_page(env_key)
        out_path = EVIDENCE_DIR / f"{key}.png"
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        result = screenshot.capture_filtered_edge_ui_view(
            page, f"{ui_base_url}{path}", engine_name, window_minutes, out_path
        )
    except Exception as e:                              # noqa: BLE001 — let the model adapt
        return f"CAPTURE FAILED — {type(e).__name__}: {str(e).splitlines()[0]}"

    caption = f"Edge UI {path.strip('/')} · {engine_name} · window={result.actual_window_label}"
    _record(key, out_path, caption)
    stats_note = ""
    if result.stats:
        stats_note = (f"\nScraped off the page: {result.stats.completed_tasks} completed "
                      f"({result.stats.completed_pct}%), {result.stats.failed_tasks} failed "
                      f"({result.stats.failed_pct}%) — use these, not fetch_engine_task_stats' "
                      f"numbers, if they differ: this is what the screenshot actually shows.")
    return (f"OK — captured, actual window applied: {result.actual_window_label}.\n"
           f"evidence_key: {key}\ncaption: {caption}{stats_note}\n"
           f"Cite \"{key}\" in a post's evidence_keys to attach it.")


def tool_download_task_and_job_logs(env_key: str, task_id: str, job_id: str) -> str:
    """Downloads the task's and job's log .zip bundles and reads the job's TDO
    id off the job-detail page. Reuses the same logged-in session as the
    screenshot tools."""
    env = _env(env_key)
    ui_base_url = edge_environments.to_ui_base_url(env)
    try:
        page = _get_page(env_key)
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        result = screenshot.download_task_and_job_logs(page, ui_base_url, task_id, job_id, EVIDENCE_DIR)
    except Exception as e:                              # noqa: BLE001
        return f"DOWNLOAD FAILED — {type(e).__name__}: {str(e).splitlines()[0]}"

    task_key, job_key = f"edge_ui_task_log_{task_id[:8]}", f"edge_ui_job_log_{job_id[:8]}"
    _record(task_key, result.task_log_path, f"task {task_id} log")
    _record(job_key, result.job_log_path, f"job {job_id} log")
    return (f"OK — downloaded.\nevidence_key: {task_key} (task log)\n"
           f"evidence_key: {job_key} (job log)\nTDO: {result.tdo_id or '(not found)'}\n"
           f"Cite these keys in a post's evidence_keys to attach the logs.")


_WINDOW_SCHEMA = {"type": "integer", "enum": screenshot.MINUTE_WINDOW_PRESETS,
                  "description": "Must be one of the Edge UI's own preset buttons"}

TOOLS = [
    {"name": "fetch_engine_task_stats",
     "description": "Per-engine task counts (completed/failed/pending/etc.) for a window in one "
                    "environment.",
     "inputSchema": {"type": "object", "properties": {
         "env_key": {"type": "string", "description": "e.g. aiw-prd5001"},
         "start_time_epoch_seconds": {"type": "integer"},
         "end_time_epoch_seconds": {"type": "integer"}},
         "required": ["env_key", "start_time_epoch_seconds", "end_time_epoch_seconds"]},
     "handler": tool_fetch_engine_task_stats},
    {"name": "list_engines_with_failures",
     "description": "Only the engines that actually have failures in this window, sorted worst "
                    "first. Start here to find out which engine an incident actually means, "
                    "instead of assuming the name in the alert title.",
     "inputSchema": {"type": "object", "properties": {
         "env_key": {"type": "string"},
         "start_time_epoch_seconds": {"type": "integer"},
         "end_time_epoch_seconds": {"type": "integer"}},
         "required": ["env_key", "start_time_epoch_seconds", "end_time_epoch_seconds"]},
     "handler": tool_list_engines_with_failures},
    {"name": "list_tasks_by_status",
     "description": "Task records matching a status (e.g. 'failed'), newest first — use to find "
                    "a representative task id/job id to inspect in detail.",
     "inputSchema": {"type": "object", "properties": {
         "env_key": {"type": "string"},
         "start_time_epoch_seconds": {"type": "integer"},
         "end_time_epoch_seconds": {"type": "integer"},
         "status": {"type": "string"}, "limit": {"type": "integer"}},
         "required": ["env_key", "start_time_epoch_seconds", "end_time_epoch_seconds", "status"]},
     "handler": tool_list_tasks_by_status},
    {"name": "get_task_detail",
     "description": "Full detail for one task, including the raw failure_reason/failure_detail "
                    "error text, job id, and org id. Pull this before concluding what the error "
                    "actually is.",
     "inputSchema": {"type": "object", "properties": {
         "env_key": {"type": "string"}, "task_id": {"type": "string"}},
         "required": ["env_key", "task_id"]},
     "handler": tool_get_task_detail},
    {"name": "get_organization_name",
     "description": "Resolve an organization id to its name.",
     "inputSchema": {"type": "object", "properties": {
         "env_key": {"type": "string"}, "organization_id": {"type": "string"}},
         "required": ["env_key", "organization_id"]},
     "handler": tool_get_organization_name},
    {"name": "list_environments",
     "description": "The aiw-xxx environment keys configured with Edge UI credentials.",
     "inputSchema": {"type": "object", "properties": {}},
     "handler": tool_list_environments},
    {"name": "capture_tasks_page_screenshot",
     "description": "Screenshot the Edge UI Tasks page filtered to one engine + window, for "
                    "attaching to the Slack thread. Logs in automatically on first use per "
                    "environment.",
     "inputSchema": {"type": "object", "properties": {
         "env_key": {"type": "string"}, "engine_name": {"type": "string"},
         "window_minutes": _WINDOW_SCHEMA, "key": {"type": "string"}},
         "required": ["env_key", "engine_name", "window_minutes"]},
     "handler": tool_capture_tasks_page_screenshot},
    {"name": "capture_engine_page_screenshot",
     "description": "Screenshot the Edge UI Engine page filtered to one engine + window.",
     "inputSchema": {"type": "object", "properties": {
         "env_key": {"type": "string"}, "engine_name": {"type": "string"},
         "window_minutes": _WINDOW_SCHEMA, "key": {"type": "string"}},
         "required": ["env_key", "engine_name", "window_minutes"]},
     "handler": tool_capture_engine_page_screenshot},
    {"name": "download_task_and_job_logs",
     "description": "Download the task's and job's log .zip bundles and read the job's TDO id. "
                    "Use a task_id/job_id you got from list_tasks_by_status or get_task_detail.",
     "inputSchema": {"type": "object", "properties": {
         "env_key": {"type": "string"}, "task_id": {"type": "string"}, "job_id": {"type": "string"}},
         "required": ["env_key", "task_id", "job_id"]},
     "handler": tool_download_task_and_job_logs},
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
    try:
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
            except Exception as e:                      # noqa: BLE001
                response = _error(request.get("id"), -32603, f"{type(e).__name__}: {e}")
            if response is not None:
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()
    finally:
        _close_all_sessions()


def _selftest() -> int:
    for name, args in (
        ("list_environments", {}),
        ("fetch_engine_task_stats", {"env_key": "aiw-prd5001",
                                     "start_time_epoch_seconds": int(time.time()) - 900,
                                     "end_time_epoch_seconds": int(time.time())}),
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
