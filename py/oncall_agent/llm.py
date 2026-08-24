import json
import os
import shutil
import subprocess
import tempfile
from typing import Optional

"""
The single place this repo talks to a model for free-text output.

Providers:
  claude_cli (default) — headless Claude Code (`claude -p --output-format
    json`). Auth is the CLI's own login in ~/.claude, so there is no API key
    and no metered credits. This is the same mechanism decide_resolution.py
    already uses for the structured engine-failure decision; keeping both on
    one provider means one login to keep alive, not two.
  anthropic — the Anthropic API directly, for when a funded key exists and a
    daemon should not depend on an interactive OAuth session.

Deliberately NOT ported from the noc-ai-lab prototype: its `gemini`
provider. That prototype ran on strictly sanitized replay data, where a free
tier that may train on inputs was an acceptable trade. This repo handles
REAL incidents — real org names, task ids, error payloads and internal
hostnames — so that trade no longer holds. Adding it back would need a paid,
no-training tier and an explicit owner decision.

Flags that matter for claude_cli, and why:
  --system-prompt (not --append-system-prompt) REPLACES Claude Code's
    coding-agent prompt, so the model gets only the NOC brief.
  --allowed-tools ""  makes this pure text-in/text-out. Tool use, evidence
    selection and remediation live in this repo's code, never in the model.
  cwd=tempdir  stops the CLI auto-discovering THIS repo's CLAUDE.md into
    every triage.
  Never add --bare: it forces ANTHROPIC_API_KEY auth and ignores the OAuth
    session, defeating the point of this provider.
"""

PROVIDER = os.environ.get("LLM_PROVIDER", "claude_cli").lower()

CLAUDE_CLI_MODEL = os.environ.get("CLAUDE_CLI_MODEL", "opus")
CLAUDE_CLI_EFFORT = os.environ.get("CLAUDE_CLI_EFFORT", "low")
CLAUDE_CLI_TIMEOUT = int(os.environ.get("CLAUDE_CLI_TIMEOUT", "300"))
ANTHROPIC_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "8000"))


def model_label() -> str:
    return CLAUDE_CLI_MODEL if PROVIDER == "claude_cli" else ANTHROPIC_MODEL


def claude_binary() -> str:
    binary = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not binary:
        raise RuntimeError(
            "LLM_PROVIDER=claude_cli but the `claude` CLI is not on PATH. Install "
            "Claude Code and run `claude` once to log in, or set CLAUDE_BIN."
        )
    return binary


def _claude_cli(system: str, user_msg: str) -> str:
    proc = subprocess.run(
        [claude_binary(), "-p", user_msg,
         "--output-format", "json",
         "--system-prompt", system,
         "--allowed-tools", "",
         "--model", CLAUDE_CLI_MODEL,
         "--effort", CLAUDE_CLI_EFFORT],
        capture_output=True, text=True,
        timeout=CLAUDE_CLI_TIMEOUT, cwd=tempfile.gettempdir(),
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:400]
        raise RuntimeError(f"claude CLI exited {proc.returncode}: {detail}")
    payload = json.loads(proc.stdout)
    if payload.get("is_error"):
        raise RuntimeError(f"claude CLI error ({payload.get('subtype')}): "
                           f"{str(payload.get('result'))[:400]}")
    return payload.get("result") or ""


def _anthropic(system: str, user_msg: str) -> str:
    from anthropic import Anthropic

    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")


def complete(system: str, user_msg: str, provider: Optional[str] = None) -> str:
    """One completion. Raises rather than returning empty — a silent empty
    triage posts as dead air in an incident thread, which is worse than a
    visible failure line."""
    provider = (provider or PROVIDER).lower()
    if provider == "claude_cli":
        out = _claude_cli(system, user_msg)
    elif provider == "anthropic":
        out = _anthropic(system, user_msg)
    else:
        raise RuntimeError(
            f"Unknown LLM_PROVIDER {provider!r} (use 'claude_cli' or 'anthropic'). "
            "See the module docstring for why 'gemini' is not wired in this repo."
        )
    if not out.strip():
        hint = ("check `claude -p` works in this shell (login, rate limits)"
                if provider == "claude_cli" else "raise LLM_MAX_TOKENS")
        raise RuntimeError(f"{model_label()} returned no text — {hint}")
    return out
