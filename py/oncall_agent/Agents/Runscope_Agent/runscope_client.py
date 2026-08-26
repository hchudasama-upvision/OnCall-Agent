import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests

"""
Real Runscope REST API client — the current-state evidence source for
synthetic-test-backed alerts (e.g. "Site DNS Valdation", "Track Job"). No
bucket/test is hardcoded: find_test_by_name scans every bucket's test names,
because there is no documented alert-name-to-test mapping anywhere.

Ported from this session's earlier architecture (the one superseded by the
2026-08-26 merge) — the API shape and the `assertions or []` fix below were
both verified against real responses, not guessed.
"""

API_BASE = "https://api.runscope.com"


def _headers() -> Dict[str, str]:
    token = os.environ.get("RUNSCOPE_API_TOKEN")
    if not token:
        raise RuntimeError("RUNSCOPE_API_TOKEN not set")
    return {"Authorization": f"Bearer {token}"}


def _get(path: str, params: Optional[dict] = None) -> dict:
    res = requests.get(f"{API_BASE}{path}", headers=_headers(), params=params, timeout=20)
    if not res.ok:
        raise RuntimeError(f"Runscope API call failed: {res.status_code} {res.reason} — {path}")
    return res.json()


def list_buckets() -> List[dict]:
    return _get("/buckets").get("data") or []


def list_tests(bucket_key: str) -> List[dict]:
    return _get(f"/buckets/{bucket_key}/tests").get("data") or []


@dataclass
class RunscopeTestRef:
    bucket_key: str
    test_id: str
    test_name: str


def find_test_by_name(name_query: str) -> Optional[RunscopeTestRef]:
    """Case-insensitive substring scan of every test name in every bucket.

    No hardcoded bucket/test mapping — one was never provided, and a wrong
    guess here would silently check the wrong test's history.
    """
    needle = name_query.lower()
    for bucket in list_buckets():
        bucket_key = bucket.get("key")
        if not bucket_key:
            continue
        for test in list_tests(bucket_key):
            name = test.get("name") or ""
            if needle in name.lower() or name.lower() in needle:
                return RunscopeTestRef(bucket_key=bucket_key, test_id=test["id"], test_name=name)
    return None


def get_latest_result_summary(ref: RunscopeTestRef) -> Optional[dict]:
    results = _get(f"/buckets/{ref.bucket_key}/tests/{ref.test_id}/results").get("data") or []
    return results[0] if results else None


def get_result_detail(ref: RunscopeTestRef, test_run_id: str) -> dict:
    return _get(f"/buckets/{ref.bucket_key}/tests/{ref.test_id}/results/{test_run_id}").get("data") or {}


@dataclass
class FailingStep:
    url: str
    failed_assertions: List[dict] = field(default_factory=list)


def extract_failing_steps(result_detail: dict) -> List[FailingStep]:
    steps: List[FailingStep] = []
    for req in result_detail.get("requests") or []:
        # `req.get("assertions", [])` is wrong here: a real response can have
        # "assertions": null explicitly present (not merely absent), which
        # `.get(..., default)` does NOT catch — it only substitutes the
        # default when the key is missing, not when the value is None.
        assertions = req.get("assertions") or []
        failed = [a for a in assertions if a.get("result") == "fail"]
        if failed:
            url = (req.get("request") or {}).get("url", "")
            steps.append(FailingStep(url=url, failed_assertions=failed))
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
    """The full lookup chain: find the test by name, pull its latest run,
    extract what actually failed. Returns None if no matching test exists —
    never fabricates a result for a test that was never found."""
    ref = find_test_by_name(name_query)
    if not ref:
        return None
    summary = get_latest_result_summary(ref)
    if not summary:
        return None
    detail = get_result_detail(ref, summary.get("test_run_id") or summary.get("id"))
    passed = sum(r.get("assertions_passed") or 0 for r in (detail.get("requests") or []))
    failed = sum(r.get("assertions_failed") or 0 for r in (detail.get("requests") or []))
    return RunscopeEvidence(
        test_name=ref.test_name,
        started_at=summary.get("started_at") or time.time(),
        overall_result=detail.get("result") or summary.get("result") or "unknown",
        assertions_passed=passed,
        assertions_failed=failed,
        failing_steps=extract_failing_steps(detail),
    )
