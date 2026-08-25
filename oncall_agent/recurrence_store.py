import json
from pathlib import Path
from typing import Optional, Tuple

"""
Tracks this pipeline's OWN prior posts about a given recurring engine/error
signature in its own posting channel, so a later occurrence can be threaded
onto that same prior post ("post it on the previous alert thread") without
needing read access to that channel — the posting bot token only has
chat:write/files:write, and the channel isn't reachable through the Slack
read connector used for historical grounding (different workspace/access —
confirmed 2026-08-24). Pure local state; no new Slack permissions required.
"""

DEFAULT_STORE_PATH = Path("dist") / "recurrence_state.json"


def make_fingerprint(env_key: str, engine_name: str, error_type: str) -> str:
    return f"{env_key}:{engine_name}:{error_type}"


def _load(store_path: Path) -> dict:
    if not store_path.exists():
        return {}
    return json.loads(store_path.read_text())


def _save(store_path: Path, data: dict) -> None:
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(json.dumps(data, indent=2))


def lookup_existing_thread(fingerprint: str, store_path: Path = DEFAULT_STORE_PATH) -> Optional[Tuple[str, str]]:
    entry = _load(store_path).get(fingerprint)
    return (entry["channel"], entry["thread_ts"]) if entry else None


def record_thread(
    fingerprint: str, channel: str, thread_ts: str, incident_number: int, store_path: Path = DEFAULT_STORE_PATH
) -> None:
    data = _load(store_path)
    data[fingerprint] = {"channel": channel, "thread_ts": thread_ts, "incident_number": incident_number}
    _save(store_path, data)
