import json
import re
from pathlib import Path
from typing import Dict, List, Optional

"""
The historical case library: 14 alert-type cases harvested from ~30 real
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

Ownership (2026-08-26): there is no single data/cases.json anymore. A case
belongs to the specialist that investigates it and lives in that specialist's
own Agents/<Name>/data/cases.json (Grafana_Agent's resource-alert cases live one
level deeper, in Agents/Grafana_Agent/grafana_metrics/data/cases.json — see
that folder for why). This module stays domain-agnostic infrastructure: it
knows how to load and query A case file, not which one. Each agent's own
prompt.py loads its own file and is the source of truth for what that agent
knows; load_merged() below exists ONLY for the two genuinely cross-domain
callers — fingerprinting before routing is known, and the generalist
fallback for alert types no specialist claims — neither of which can know in
advance which single agent's data applies.

One alert type is deliberately in two files: KubePersistentVolumeFillingUp,
which routes to the Kubernetes specialist but also has a graph-side entry in
the Grafana agent (owner's call, 2026-08-26 — see either entry's
_shared_with, which says so and warns that both copies have to be edited
together). The merged view therefore returns both, which is fine: it is only
used for fingerprinting and the generalist fallback, and the two entries
describe the same alert from different angles.
"""


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
                bucket = self.index.setdefault(name.lower(), [])
                # One case can yield two names that differ only in case —
                # "AlbUnhealthyHostCritical" from the fingerprint and
                # "ALBUnhealthyHostCritical" from the alertname — and both
                # lower-case to the same key, which appended the case twice and
                # sent it to the model twice (found 2026-08-31).
                if not any(existing is case for existing in bucket):
                    bucket.append(case)
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

    def _suffix_match(self, fingerprint: str) -> List[dict]:
        """Cases for an alertname the key merely ENDS with.

        The same treatment evidence_panels.PanelMap._suffix_match applies to
        the panel map, and for the same reason: PandoLogic renders the host
        into the alertname, so the line reads "PandoLogic - SVC120
        DiskSpaceUtilizationWarning", the parsed alert name is "SVC120
        DiskSpaceUtilizationWarning", and an exact lookup on
        "DiskSpaceUtilizationWarning" misses — silently, returning no cases at
        all for a whole alert family (found on a real dry run, 2026-08-26).
        Anchored on a word boundary and longest-name-first, so
        "...Critical" can never be satisfied by an entry for "...Warning".
        """
        fp = (fingerprint or "").lower()
        if not fp:
            return []
        for name in self.known_alertnames:          # already longest-first
            candidate = name.lower()
            if fp.endswith(" " + candidate):
                return self.index.get(candidate) or []
        # ...and the mirror case: the alertname sits INSIDE a longer alert
        # title. "US-Prod Response Codes - nginx -ai13s   aiWARE/prod" ends with
        # the environment, not with the alertname, so the suffix rule above
        # misses it and the entry never reaches the prompt (found on a real run,
        # 2026-08-28). Longest-first, and only after exact and suffix have
        # failed, so a more specific entry still wins.
        for name in self.known_alertnames:
            candidate = name.lower()
            if len(candidate) >= 8 and candidate in fp:
                return self.index.get(candidate) or []
        return []

    def find(self, fingerprint: str, limit: int = 2) -> List[dict]:
        """Past cases whose fingerprint matches the alertname."""
        hits = self.index.get((fingerprint or "").lower()) or self._suffix_match(fingerprint)
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


def load_case_library(path: Path) -> CaseLibrary:
    """Load ONE agent's own case file. There is no default path — a caller
    that doesn't know which agent it means shouldn't silently get one."""
    if not path.exists():
        return CaseLibrary([])
    return CaseLibrary(json.loads(path.read_text()))


def load_merged(paths: List[Path]) -> CaseLibrary:
    """Every agent's cases in one library, for the two callers that
    legitimately need to see across all of them: fingerprinting an alert
    before routing has picked a specialist, and the generalist fallback for
    an alert type no specialist claims. NOT for a specialist's own prompt —
    that should only ever see its own agent's cases (its own
    Agents/<Name>/data/cases.json), not a sibling's."""
    cases: List[dict] = []
    for path in paths:
        if path.exists():
            cases.extend(json.loads(path.read_text()))
    return CaseLibrary(cases)
