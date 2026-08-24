import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, TypeVar

from playwright.sync_api import Page, sync_playwright

"""
Ported from the TypeScript version (src/evidence/screenshot.ts). Behavior is
intentionally kept 1:1, including bug fixes discovered against the real
Edge UI:
  - session cookie login (not the static Bearer token) for browser pages;
    must wait for the actual post-login redirect away from /auth/login/,
    not just domcontentloaded.
  - the time-range control is a plain button/[role=button] whose own text is
    "<n><unit> ago" — found by DOM scan, not by a fragile text locator,
    because a naive text match also matches unrelated relative-timestamp
    table cells.
  - setting the window is occasionally flaky (animation timing) — retried.
  - stat cards can lag the filter update — scraped with retries.
  - "Download log" only, never "Full download".
  - TDO id can render as one concatenated leaf ("TDO4280322462") or as a
    label cell + separate numeric cell in the same row (confirmed via
    screenshot 2026-08-24) — both handled.
"""

MINUTE_WINDOW_PRESETS = [5, 10, 15, 30, 45]

T = TypeVar("T")


@contextmanager
def with_edge_ui_session(ui_base_url: str, username: str, password: str):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            page = context.new_page()
            page.goto(f"{ui_base_url}/auth/login/", wait_until="domcontentloaded", timeout=20000)
            page.fill("#username", username)
            page.fill("#password", password)
            page.click('button[type="submit"]')
            # The click submits an async login request rather than a normal
            # navigation, so anything right after it fires too early — wait
            # for the actual post-login redirect away from /auth/login/.
            page.wait_for_url(lambda url: "/auth/login" not in url, timeout=20000)
            yield page
        finally:
            browser.close()


_FIND_TIME_CONTROL_RECT_JS = """
() => {
  let found;
  document.querySelectorAll("button, div[role='button']").forEach((el) => {
    const t = (el.textContent || "").trim();
    if (/^\\d+[a-z]*\\s*ago$/i.test(t)) found = el.getBoundingClientRect();
  });
  return found ? { x: found.x, y: found.y, w: found.width, h: found.height } : undefined;
}
"""

_READ_TIME_CONTROL_LABEL_JS = """
() => {
  let label;
  document.querySelectorAll("button, div[role='button']").forEach((el) => {
    const t = (el.textContent || "").trim();
    if (/^\\d+[a-z]*\\s*ago$/i.test(t)) label = t;
  });
  return label;
}
"""

_SCRAPE_TASK_STATS_JS = """
() => ["Completed tasks", "Failed Tasks"].map((label) => {
  let match = null;
  document.querySelectorAll("*").forEach((el) => {
    if (match) return;
    const t = (el.textContent || "").trim();
    if (t !== label || el.children.length !== 0) return;
    let cur = el;
    for (let i = 0; i < 10 && cur; i++) {
      const m = (cur.textContent || "").trim().match(new RegExp(`^(\\\\d+)${label}(\\\\d+)%$`));
      if (m) { match = m; break; }
      cur = cur.parentElement;
    }
  });
  return match ? { count: Number(match[1]), pct: Number(match[2]) } : undefined;
})
"""

_SCRAPE_TDO_ID_JS = """
() => {
  let tdo;
  document.querySelectorAll("*").forEach((el) => {
    if (tdo || el.children.length !== 0) return;
    const t = (el.textContent || "").trim();
    const concatenated = t.match(/^TDO(\\d+)$/);
    if (concatenated) { tdo = concatenated[1]; return; }
    if (t !== "TDO") return;
    let row = el.parentElement;
    for (let i = 0; i < 4 && row && !tdo; i++) {
      const leaves = Array.from(row.querySelectorAll("*")).filter((c) => c.children.length === 0);
      const valueCell = leaves.find((c) => c !== el && /^\\d+$/.test((c.textContent || "").trim()));
      if (valueCell) tdo = valueCell.textContent.trim();
      row = row.parentElement;
    }
  });
  return tdo;
}
"""


@dataclass
class ScrapedTaskStats:
    completed_tasks: int
    completed_pct: int
    failed_tasks: int
    failed_pct: int


def _scrape_task_stats(page: Page) -> Optional[ScrapedTaskStats]:
    completed, failed = page.evaluate(_SCRAPE_TASK_STATS_JS)
    if not completed or not failed:
        return None
    return ScrapedTaskStats(
        completed_tasks=completed["count"],
        completed_pct=completed["pct"],
        failed_tasks=failed["count"],
        failed_pct=failed["pct"],
    )


@dataclass
class CaptureResult:
    actual_window_label: str
    stats: Optional[ScrapedTaskStats]


def capture_filtered_edge_ui_view(
    page: Page, page_url: str, engine_name: str, window_minutes: int, out_path: Path
) -> CaptureResult:
    """
    Filters the Tasks or Engine dashboard to one engine + a relative minute
    window (matching the filter controls you'd use by hand) and screenshots
    the result. On views that group results behind a collapsible per-engine
    section (e.g. /processing/engine/), expands that section and scrolls it
    into view so the actual charts are captured, not the summary widget above.
    """
    if window_minutes not in MINUTE_WINDOW_PRESETS:
        raise ValueError(
            f"capture_filtered_edge_ui_view only supports minute presets {MINUTE_WINDOW_PRESETS} (got {window_minutes})"
        )

    page.goto(page_url, wait_until="domcontentloaded", timeout=20000)
    page.wait_for_timeout(800)

    engine_select = page.locator(".ant-select", has_text="Type to search engine").first
    engine_select.wait_for(state="visible", timeout=15000)
    engine_select.click()
    engine_select.locator("input").fill(engine_name)
    option = page.locator(".ant-select-item-option-content", has_text=engine_name).first
    option.wait_for(state="visible", timeout=10000)
    option.click()
    page.keyboard.press("Escape")
    page.wait_for_timeout(500)

    # Setting the window is occasionally flaky (animation-timing dependent —
    # the panel doesn't always register the click), so verify it actually
    # took and retry a few times rather than silently proceeding with the
    # wrong window.
    expected_prefix = str(window_minutes)
    applied_label: Optional[str] = None
    for _attempt in range(3):
        try:
            rect = page.evaluate(_FIND_TIME_CONTROL_RECT_JS)
            if not rect:
                raise RuntimeError(f"Time-range control not found on {page_url}")
            page.mouse.click(rect["x"] + rect["w"] / 2, rect["y"] + rect["h"] / 2)
            page.wait_for_timeout(800)
            # data-interval/data-type are the preset button's real attributes —
            # more stable than matching by visible name, which is ambiguous
            # ("1"/"2"/"3" appear in the hours AND days rows too).
            minute_button = page.locator(f'button[data-interval="{window_minutes}"][data-type="minutes"]')
            minute_button.wait_for(state="visible", timeout=5000)
            minute_button.click(force=True)
            page.keyboard.press("Escape")
            page.wait_for_timeout(1500)

            applied_label = page.evaluate(_READ_TIME_CONTROL_LABEL_JS)
            if applied_label and applied_label.startswith(expected_prefix):
                break
        except Exception:
            # The panel sometimes doesn't open on a given click (animation
            # timing) — fall through and retry rather than aborting on the
            # first miss.
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass

    if not applied_label or not applied_label.startswith(expected_prefix):
        raise RuntimeError(
            f'Could not set the {window_minutes}-minute window on {page_url} after 3 attempts (stuck at "{applied_label}")'
        )

    collapse_header = page.locator(".ant-collapse-header", has_text=engine_name).first
    if collapse_header.count() > 0:
        collapse_header.click()
        page.wait_for_timeout(1500)
        content = page.locator(".ant-collapse-content").first
        content.evaluate("(el) => el.scrollIntoView({ block: 'end' })")
        page.wait_for_timeout(500)

    actual_window_label = page.evaluate(_READ_TIME_CONTROL_LABEL_JS)

    # The stat cards can lag a moment behind the window/engine filter update —
    # retry a few times rather than settling for None on first miss.
    stats: Optional[ScrapedTaskStats] = None
    for attempt in range(4):
        if stats:
            break
        if attempt > 0:
            page.wait_for_timeout(800)
        stats = _scrape_task_stats(page)

    # The mouse is still hovering the last-clicked control at this point,
    # which triggers a hover tooltip that would show up in the screenshot but
    # isn't part of the real page — move it away first.
    page.mouse.move(10, 10)
    page.wait_for_timeout(300)
    page.screenshot(path=str(out_path))

    return CaptureResult(actual_window_label=actual_window_label or f"{window_minutes} minutes", stats=stats)


def _download_log_via_action_menu(page: Page, out_path: Path) -> None:
    """Clicks Action -> "Download log" (never "Full download") and saves the resulting file."""
    action_button = page.get_by_role("button", name="Action").first
    action_button.wait_for(state="visible", timeout=10000)
    action_button.click()
    page.wait_for_timeout(400)

    with page.expect_download(timeout=15000) as download_info:
        page.get_by_text("Download log", exact=True).click()
    download = download_info.value
    download.save_as(str(out_path))


def _scrape_tdo_id(page: Page) -> Optional[str]:
    return page.evaluate(_SCRAPE_TDO_ID_JS)


@dataclass
class LogDownloadResult:
    task_log_path: Path
    job_log_path: Path
    tdo_id: Optional[str]


def download_task_and_job_logs(
    page: Page, ui_base_url: str, task_id: str, job_id: str, out_dir: Path
) -> LogDownloadResult:
    """
    Downloads the task's and job's log bundles (each a .zip containing engine
    logs, ffmpeg logs, and IO configs) via the same "Download log" action a
    human would click, and reads the job's TDO id off the job-detail page.
    Only ever clicks "Download log" — "Full download" is a separate, larger
    export this function must not trigger.
    """
    page.goto(f"{ui_base_url}/processing/tasks/detail/?taskId={task_id}", wait_until="domcontentloaded", timeout=20000)
    page.wait_for_timeout(1200)
    task_log_path = out_dir / f"task-{task_id}.zip"
    _download_log_via_action_menu(page, task_log_path)

    page.goto(f"{ui_base_url}/processing/jobs/detail/?jobId={job_id}", wait_until="domcontentloaded", timeout=20000)
    page.wait_for_timeout(1200)
    tdo_id = _scrape_tdo_id(page)
    job_log_path = out_dir / f"job-{job_id}.zip"
    _download_log_via_action_menu(page, job_log_path)

    return LogDownloadResult(task_log_path=task_log_path, job_log_path=job_log_path, tdo_id=tdo_id)
