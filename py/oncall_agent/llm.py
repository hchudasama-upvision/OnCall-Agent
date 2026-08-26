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
  --tools ""  (NOT --allowed-tools/--allowedTools) is what actually strips
    tool availability for pure text-in/text-out. --allowedTools only
    pre-approves tools to skip the permission prompt — it does not remove
    them from the model's context, so in a non-interactive -p run the model
    can still see and attempt tools, get silently denied (no TTY to
    approve), and waste effort on failed calls before answering. That was
    the previous bug here: this file used --allowed-tools "" and got
    "restricted but still tool-aware, sometimes-flaky" output instead of
    clean text-only completion. --tools "" removes them from the session
    entirely. Tool use, evidence selection and remediation live in this
    repo's code, never in the model — verify with `claude --help` on your
    installed version if CLI behavior ever seems to drift from this.
  --effort  set as high as the installed CLI supports (see
    CLAUDE_CLI_EFFORT below) — triage quality tracks reasoning depth far
    more than it tracks anything prompt wording can buy you.
  cwd=tempdir  stops the CLI auto-discovering THIS repo's CLAUDE.md into
    every triage.
  Never add --bare: it forces ANTHROPIC_API_KEY auth and ignores the OAuth
    session, defeating the point of this provider.

On "creative": incident triage wants maximum *reasoning depth*, not output
variance — those are different knobs. --effort (claude_cli) and thinking
budget (anthropic) buy you the former. Temperature buys you the latter, and
turning it up on root-cause analysis mostly buys you confident-sounding
wrong guesses. It's exposed below for the anthropic path because it was
asked for, defaulted low, and logged when raised so a bad triage is
traceable back to the setting that produced it.
"""

PROVIDER = os.environ.get("LLM_PROVIDER", "claude_cli").lower()

CLAUDE_CLI_MODEL = os.environ.get("CLAUDE_CLI_MODEL", "opus")
# Highest reasoning depth the CLI exposes. Observed valid values: low, medium,
# high, xhigh, max — "max" has been gated to Opus-class models in some CLI
# versions. Confirm against `claude --help` on your installed version; if
# "max" is rejected there, drop to "xhigh" or "high".
CLAUDE_CLI_EFFORT = os.environ.get("CLAUDE_CLI_EFFORT", "max")
CLAUDE_CLI_TIMEOUT = int(os.environ.get("CLAUDE_CLI_TIMEOUT", "300"))

ANTHROPIC_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "16000"))
# Extended thinking budget in tokens for the anthropic provider — the API
# equivalent of --effort. 0 disables thinking entirely. Must stay below
# MAX_TOKENS (the API requires max_tokens > thinking budget).
ANTHROPIC_THINKING_BUDGET = int(os.environ.get("ANTHROPIC_THINKING_BUDGET", "10000"))
# Real creativity/variance knob, anthropic path only. Default low on purpose —
# see module docstring. Raise deliberately, not as a default-on "smarter" dial.
ANTHROPIC_TEMPERATURE = float(os.environ.get("ANTHROPIC_TEMPERATURE", "0.2"))


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
         "--tools", "",
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
    kwargs = dict(
        model=ANTHROPIC_MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    if ANTHROPIC_THINKING_BUDGET > 0:
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": ANTHROPIC_THINKING_BUDGET}
        # Temperature must be left at its API default (1) when thinking is
        # enabled — the API rejects a custom temperature alongside thinking.
        # Depth (thinking) wins over variance (temperature) here on purpose.
    else:
        kwargs["temperature"] = ANTHROPIC_TEMPERATURE

    resp = client.messages.create(**kwargs)
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