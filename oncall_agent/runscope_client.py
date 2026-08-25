import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests

"""
Real Runscope evidence-gathering — confirmed 2026-08-24 by locating the
actual "Site DNS Valdation" test (bucket 2s1miwzse4pv, test
bd69b4bc-7b0a-47d7-9bc2-689bd5204de3) via the documented public Runscope
REST API (api.runscope.com) and reading its latest real run. This gives
alert types backed by a Runscope test real CURRENT evidence (which URL/step
is failing right now and why), not just historical pattern-matching from
Slack — the same role Edge UI evidence plays for engine-failure alerts.
"""

API_BASE = "https://api.runscope.com"


def _headers() -> Dict[str, str]:
    token = os.environ["RUNSCOPE_API_TOKEN"]
    return {"Authorization": f"Bearer {token}"}


def list_buckets() -> List[dict]:
    res = requests.get(f"{API_BASE}/buckets", headers=_headers(), timeout=20)
    res.raise_for_status()
    return res.json()["data"]


def list_tests(bucket_key: str) -> List[dict]:
    res = requests.get(f"{API_BASE}/buckets/{bucket_key}/tests", headers=_headers(), timeout=20)
    res.raise_for_status()
    return res.json()["data"]


@dataclass
class RunscopeTestRef:
    bucket_key: str
    test_id: str
    test_name: str


def find_test_by_name(name_query: str) -> Optional[RunscopeTestRef]:
    """Case-insensitive substring search for a test name across all buckets — no hardcoded bucket/test mapping."""
    query = name_query.lower()
    for bucket in list_buckets():
        for test in list_tests(bucket["key"]):
            if query in test.get("name", "").lower():
                return RunscopeTestRef(bucket_key=bucket["key"], test_id=test["id"], test_name=test["name"])
    return None


def get_latest_result_summary(ref: RunscopeTestRef) -> Optional[dict]:
    res = requests.get(f"{API_BASE}/buckets/{ref.bucket_key}/tests/{ref.test_id}/results", headers=_headers(), timeout=20)
    res.raise_for_status()
    results = res.json()["data"]
    return results[0] if results else None


def get_result_detail(ref: RunscopeTestRef, test_run_id: str) -> dict:
    res = requests.get(
        f"{API_BASE}/buckets/{ref.bucket_key}/tests/{ref.test_id}/results/{test_run_id}",
        headers=_headers(),
        timeout=20,
    )
    res.raise_for_status()
    return res.json()["data"]


@dataclass
class FailingStep:
    url: str
    failed_assertions: List[dict] = field(default_factory=list)


def extract_failing_steps(result_detail: dict) -> List[FailingStep]:
    # `assertions` (and `requests`) can be present-but-null in real API
    # responses, not just absent — `.get(key, [])` doesn't catch that.
    steps = []
    for req in result_detail.get("requests") or []:
        failed = [a for a in (req.get("assertions") or []) if a.get("result") == "fail"]
        if failed:
            steps.append(FailingStep(url=req.get("url", ""), failed_assertions=failed))
    return steps


@dataclass
class RunscopeEvidence:
    test_name: str
    started_at: float
    overall_result: str
    assertions_passed: int
    assertions_failed: int
    failing_steps: List[FailingStep]


def fetch_current_evidence(name_query: str) -> Optional[RunscopeEvidence]:
    """
    Finds a Runscope test whose name matches this alert type and returns its
    most recent run's failure detail, or None if no matching test exists
    (this alert type isn't Runscope-backed) or the latest run has no
    results yet.
    """
    ref = find_test_by_name(name_query)
    if not ref:
        return None
    summary = get_latest_result_summary(ref)
    if not summary:
        return None
    detail = get_result_detail(ref, summary["test_run_id"])
    return RunscopeEvidence(
        test_name=ref.test_name,
        started_at=summary["started_at"],
        overall_result=summary["result"],
        assertions_passed=summary["assertions_passed"],
        assertions_failed=summary["assertions_failed"],
        failing_steps=extract_failing_steps(detail),
    )
