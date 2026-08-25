import json
import re
from pathlib import Path
from typing import Dict, List, Optional

"""
The historical case library: 10 alert-type cases harvested from ~30 real
VictorOps incident threads in #comms-noc and sanitized (no real IPs,
hostnames, instance ids, tokens, employee or customer names). Ported from the
noc-ai-lab prototype, where it was the crown jewel of the triage quality.

This is the OFFLINE half of "investigate based on #comms-noc": curated
tribal knowledge that survives Slack's retention window and does not need
channels:history to read. slack_history.py is the LIVE half — the actual
recent threads. Triage uses both.

Per-case fields: fingerprint, alertname, alert_pattern, environment,
occurrences_observed, typical_resolution_minutes, diagnosis_steps,
resolution_steps, gotchas, escalation, example_commands. The gotchas encode
what a runbook never says out loud (e.g. "reboot, don't restart nfs-server —
D-state processes never clear").
"""

_DATA_PATH = Path(__file__).resolve().parents[2] / "data" / "cases.json"


def _case_names(case: dict) -> set:
    """Every alertname string a raw alert could contain for this case.

    Derived from the library itself rather than a hand-kept list, which
    drifted in the prototype (DiskSpaceUtilizationWarning vs the library's
    ...Critical, KubePodNotReady vs KubePodsNotReady, and 5 of 10 cases
    missing). One alertname can pack several real Alertmanager names
    ("KubePodsNotReady / KubePodCrashLooping") or an exporter suffix
    ("... (windows_exporter)").
    """
    names = {case["fingerprint"].split("/")[0]}
    for part in case["alertname"].split("/"):
        part = re.sub(r"\([^)]*\)", "", part).strip()
        if len(part) >= 5:
            names.add(part)
    return names


class CaseLibrary:
    def __init__(self, cases: List[dict]):
        self.cases = cases
        self.index: Dict[str, List[dict]] = {}
        for case in cases:
            for name in _case_names(case):
                self.index.setdefault(name.lower(), []).append(case)
        # longest first, so the most specific alertname in the library wins
        self.known_alertnames = sorted(
            {n for c in cases for n in _case_names(c)}, key=len, reverse=True
        )

    def fingerprint(self, alert_text: str) -> str:
        """Stable alert fingerprint (alertname; instance details stripped)."""
        for name in self.known_alertnames:
            if name.lower() in alert_text.lower():
                return name
        # fallback: first token after "[FIRING:n]"
        m = re.search(r"\[FIRING:\d+\]\s*([^\n(]{5,60})", alert_text)
        return m.group(1).strip() if m else "unknown"

    def find(self, fingerprint: str, limit: int = 2) -> List[dict]:
        """Past cases whose fingerprint matches the alertname."""
        hits = self.index.get((fingerprint or "").lower())
        if not hits:  # regex-derived fingerprint: fall back to substring match
            fp = (fingerprint or "").lower()
            hits = [
                c for c in self.cases
                if fp and (fp in c["fingerprint"].lower() or fp in c["alertname"].lower())
            ]
        return hits[:limit]

    def keywords_for(self, fingerprint: str) -> List[str]:
        """Search terms for pulling matching live #comms-noc threads.

        The fingerprint itself plus each matching case's bare alertname, so a
        compound library entry still finds threads written with either half.
        """
        keywords = {fingerprint} if fingerprint and fingerprint != "unknown" else set()
        for case in self.find(fingerprint, limit=len(self.cases)):
            keywords |= _case_names(case)
        return sorted(keywords)


def load_case_library(path: Path = _DATA_PATH) -> CaseLibrary:
    if not path.exists():
        return CaseLibrary([])
    return CaseLibrary(json.loads(path.read_text()))
