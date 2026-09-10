import json
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from .. import agent_memory
from ..investigate import (
    DEFAULT_MCP_CONFIG,
    Investigation,
    _diagnose_cli_failure,
    _extract_stream,
    _resolve_mcp_config,
    _run_evidence_dir,
    _schema,
    _validate,
    build_prompt,
)
from ..types import ParsedAlert
from .AWS_Agent import prompt as aws_prompt
from .Edgeui_Agent import prompt as edge_ui_prompt, server as edge_ui_server
from .Grafana_Agent import server as grafana_mcp
from .Grafana_Agent.grafana_metrics import prompt as grafana_metrics_prompt
from .K8S_Agent import prompt as kubernetes_prompt
from .Runscope_Agent import prompt as runscope_prompt

"""
Domain-specialist dispatch: one focused `claude -p` subprocess call per
specialist, instead of investigate.py's one generalist call with every tool
at once. Reuses investigate.py's proven machinery (subprocess construction,
--output-format stream-json corpus capture, the no-fabrication URL/
evidence-key/timestamp validation, the JSON schema) — parameterized on tool
list and system prompt instead of hardcoded to Slack+Grafana.

MASTER_Agent/router.py picks the specialist name; each Agents/<Name>/ folder
owns what that specialist actually is: its tools and case library in
prompt.py, its own MCP server.py where it has one. This module only
assembles those into SPECIALISTS and runs the call — it does not itself
define any specialist's tools, prompt, or data.

Each specialist also gets its own write-back memory (agent_memory.py): what
it actually found the last few times it saw this exact alert type, scoped
per specialist so one agent's history never leaks into another's prompt.
"""

SPECIALISTS: Dict[str, dict] = {
    "edge_ui": {"tools": edge_ui_prompt.TOOLS, "system_prompt": edge_ui_prompt.SYSTEM_PROMPT,
               "case_library": edge_ui_prompt.CASE_LIBRARY},
    "kubernetes": {"tools": kubernetes_prompt.TOOLS, "system_prompt": kubernetes_prompt.SYSTEM_PROMPT,
                  "case_library": kubernetes_prompt.CASE_LIBRARY},
    "grafana_metrics": {"tools": grafana_metrics_prompt.TOOLS,
                        "system_prompt": grafana_metrics_prompt.SYSTEM_PROMPT,
                        "case_library": grafana_metrics_prompt.CASE_LIBRARY},
    "runscope": {"tools": runscope_prompt.TOOLS, "system_prompt": runscope_prompt.SYSTEM_PROMPT,
                "case_library": runscope_prompt.CASE_LIBRARY},
    "aws": {"tools": aws_prompt.TOOLS, "system_prompt": aws_prompt.SYSTEM_PROMPT,
            "case_library": aws_prompt.CASE_LIBRARY},
}


def run_specialist(
    alert: ParsedAlert,
    specialist: str,
    past_cases: Optional[List[dict]] = None,
    evidence_keys: Optional[List[str]] = None,
    evidence_descriptions: Optional[Dict[str, str]] = None,
    panel_hint: str = "",
    fingerprint: Optional[str] = None,
    mcp_config: Path = DEFAULT_MCP_CONFIG,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
    claude_binary: Optional[str] = None,
) -> Investigation:
    """Run one domain specialist's tool-using investigation and return a
    validated Decision — same contract as investigate.investigate_alert(),
    parameterized by domain instead of one fixed tool/prompt bundle."""
    import shutil

    config = SPECIALISTS.get(specialist)
    if not config:
        raise RuntimeError(f"Unknown specialist {specialist!r} — known: {sorted(SPECIALISTS)}")

    evidence_keys = list(evidence_keys or [])
    binary = claude_binary or os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not binary:
        raise RuntimeError("`claude` CLI not found — set CLAUDE_BIN or install Claude Code")
    if not Path(mcp_config).exists():
        raise RuntimeError(f"MCP config not found: {mcp_config}")
    resolved_config = _resolve_mcp_config(Path(mcp_config))

    # Per-run directory so concurrent investigations cannot read or clobber
    # each other's rendered panels/screenshots.
    run_dir = _run_evidence_dir(alert)
    grafana_mcp.clear_manifest(run_dir)
    edge_ui_server.clear_manifest(run_dir)

    fingerprint = fingerprint or alert.alert_name or alert.fingerprint
    memory_records = agent_memory.recall(specialist, fingerprint)

    prompt = build_prompt(alert, evidence_keys, evidence_descriptions, past_cases, memory_records)
    if panel_hint:
        prompt += f"\n\nHINT (not a substitute for checking): {panel_hint}"

    tools = config["tools"]
    cmd = [
        binary, "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--system-prompt", config["system_prompt"],
        "--mcp-config", str(resolved_config), "--strict-mcp-config",
        "--allowed-tools", *tools,
        "--json-schema", json.dumps(_schema(evidence_keys)),
        "--model", model or os.environ.get("CLAUDE_CLI_MODEL", "opus"),
        "--effort", effort or os.environ.get("CLAUDE_CLI_EFFORT", "medium"),
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True,
        timeout=timeout_seconds or int(os.environ.get("INVESTIGATE_TIMEOUT", "900")),
        cwd=str(Path(__file__).resolve().parents[3]),
        stdin=subprocess.DEVNULL,
        env={**os.environ, "CLAUDE_DISABLE_PROJECT_CONTEXT": "1", "EVIDENCE_DIR": str(run_dir)},
    )
    if proc.returncode != 0:
        raise RuntimeError(_diagnose_cli_failure(proc.returncode, proc.stdout, proc.stderr))

    structured, corpus, tools_used, meta = _extract_stream(proc.stdout)
    if meta.get("is_error"):
        raise RuntimeError(f"claude CLI reported an error: {str(meta.get('result'))[:400]}")
    if structured is None:
        raise RuntimeError(f"No structured output returned. Tail: {proc.stdout[-500:]}")

    # Whatever the model actually rendered/captured is now valid evidence, on
    # top of anything the caller supplied. A key it did NOT produce still fails.
    rendered = {**grafana_mcp.load_manifest(run_dir), **edge_ui_server.load_manifest(run_dir)}
    evidence_files = {k: Path(v["path"]) for k, v in rendered.items()}
    decision, prior = _validate(structured, alert, corpus,
                                evidence_keys + list(rendered), past_cases, memory_records)

    # Only a validated decision is worth remembering — a run that raised
    # above never reaches here, so a failure can't get memorized as if it
    # were a finding.
    agent_memory.remember(specialist, fingerprint, agent_memory.build_record(alert, decision, fingerprint))

    return Investigation(
        decision=decision,
        prior_incidents=prior,
        tools_used=tools_used,
        sources_seen=len(corpus.split("---")) if corpus else 0,
        cost_usd=float(meta.get("total_cost_usd") or 0.0),
        duration_ms=int(meta.get("duration_api_ms") or 0),
        evidence_files=evidence_files,
    )
