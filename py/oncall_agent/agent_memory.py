import json
import re
import time
from pathlib import Path
from typing import List, Optional

"""
Agent-wise write-back memory (2026-08-26): each specialist remembers its own
real past investigations of a given alert type, and gets them back at the
start of its next one — the piece the case library alone never provided.
`data/cases.json` is curated tribal knowledge someone wrote down once;
this is what the agent itself actually found, run after run, for THIS exact
alert type, on its own.

Split by specialist AND by fingerprint (one JSONL file per alert type per
agent) so recall is a single file read, not a scan-and-filter over every
incident this agent has ever seen. Lives under .state/ — like .state/audit/
and .state/mcp-resolved.json, this is accumulated runtime state, not
source-controlled knowledge, and it is expected to grow, drift, and
occasionally be wrong. That is exactly why every record is written with
should_post and root_cause_narrative rather than as an instruction: a
specialist is told to VERIFY a memory against current evidence, the same
guarded status case-library entries already get (see shared_prompt.py's
LINKING_RULE/GUARDRAILS) — memory is a head start, never a fact.

Nothing here is fed into a prompt un-labelled: format_memory() below is
built to sit next to investigate.py's format_cases(), under its own
explicitly-verify-this heading, never merged into the case-library text.
"""

_MEMORY_ROOT = Path(__file__).resolve().parents[2] / ".state" / "memory"


def _safe_name(fingerprint: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (fingerprint or "unknown").lower()).strip("_")
    return slug or "unknown"


def _memory_path(specialist: str, fingerprint: str) -> Path:
    return _MEMORY_ROOT / specialist / f"{_safe_name(fingerprint)}.jsonl"


def remember(specialist: str, fingerprint: str, record: dict) -> None:
    """Append one real, validated investigation outcome. Called only after
    a decision has passed _validate() — never on a failed/aborted run, so
    memory can't accumulate a crash as if it were a finding."""
    path = _memory_path(specialist, fingerprint)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")


def recall(specialist: str, fingerprint: str, limit: int = 3) -> List[dict]:
    """The most recent real investigations of this exact alert type by this
    agent, oldest of the returned set first (so the prompt reads chronologically)."""
    path = _memory_path(specialist, fingerprint)
    if not path.exists():
        return []
    records = []
    for line in path.read_text().splitlines()[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def build_record(alert, decision, fingerprint: str) -> dict:
    """What's worth remembering from one investigation — deliberately not
    the full posts array (that's per-run evidence-key bookkeeping, not a
    durable fact) and not the full reasoning (verbose, internal-only)."""
    return {
        "recorded_at": time.time(),
        "incident_number": alert.incident_number,
        "alert_name": alert.alert_name or alert.incident_name,
        "environment_key": alert.environment_key,
        "fingerprint": fingerprint,
        "should_post": decision.should_post,
        "root_cause_narrative": decision.root_cause_narrative,
        "owning_team_mention": decision.owning_team_mention,
    }


def format_memory(records: Optional[List[dict]]) -> str:
    """For the prompt — mirrors investigate.py's format_cases() but is
    explicitly labelled as THIS agent's own real history, not curated data,
    and explicitly not authoritative for the current incident."""
    if not records:
        return ("(no memory yet — this agent has not investigated this exact alert type "
                "before, or memory was cleared. Investigate from scratch.)")
    return json.dumps(records, indent=2)
