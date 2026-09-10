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

# The Backlog card on /processing/jobs/: an Ant Design card whose head title
# reads "Backlog" (verified against a real environment 2026-08-31). Selected by
# its TITLE rather than a generated class — `TimeSeriesChart_cardChart__9AQva`
# is a build-hashed CSS-module name and will change on the next Edge UI deploy.
_FIND_BACKLOG_CARD_JS = """
() => {
  const title = Array.from(document.querySelectorAll('.ant-card-head-title'))
    .find(el => /^backlog\\b/i.test((el.innerText || '').trim()));
  if (!title) return null;
  const card = title.closest('.ant-card');
  if (!card) return null;
  card.scrollIntoView({block: 'center'});
  return true;
}
"""

# Drawn content, not just a mounted card: ApexCharts renders an <svg> with paths
# once the data arrives, and the card shows an ant-spin spinner until then.
# Screenshotting on a timer produced a picture of the spinner.
_BACKLOG_READY_JS = """
() => {
  const title = Array.from(document.querySelectorAll('.ant-card-head-title'))
    .find(el => /^backlog\\b/i.test((el.innerText || '').trim()));
  if (!title) return false;
  const card = title.closest('.ant-card');
  if (!card) return false;
  if (card.querySelector('.ant-spin-spinning')) return false;
  const paths = card.querySelectorAll('svg path, svg rect, svg circle');
  const legend = card.querySelectorAll('.apexcharts-legend-text');
  return paths.length > 3 || legend.length > 0;
}
"""

def capture_backlog_card(page: Page, out_path: Path, ui_base_url: str,
                         engine: str = "", minutes: int = 360,
                         timeout_ms: int = 45000) -> dict:
    """Screenshot the Edge UI "Backlog" card, optionally for ONE engine.

    `engine` uses the card's own behaviour, which the owner pointed out and
    measurement confirmed: clicking an engine name in the ApexCharts legend
    ISOLATES that series (36 legend entries and 108 paths become 1 and 3) and
    clicking again restores all of them. It is NOT a per-series hide toggle —
    the first attempt here assumed it was, clicked every other engine to hide
    them, and left the chart blank with the x-axis collapsed to 00:00:00.
    One click, then verify.

    Returns {"engine", "series_shown", "same_name_series"} so the caller can
    caption honestly: several series can share one engine name (they differ by
    queue priority), and then the card shows one of them.
    """
    # A taller viewport than the session default (1280x800) is what actually
    # keeps the sticky filter bar out of the frame: the card is ~485px tall, so
    # centring it in an 800px window leaves its top row under the bar, and the
    # bar's engine chip then shows in the clip's top-right corner. Centring it
    # in 1200px clears the bar without touching the page.
    page.set_viewport_size({"width": 1440, "height": 1200})
    page.goto(f"{ui_base_url}/processing/jobs/", wait_until="networkidle", timeout=timeout_ms)
    if not page.evaluate(_FIND_BACKLOG_CARD_JS):
        raise RuntimeError("no card titled 'Backlog' on /processing/jobs/ — the Edge UI "
                           "layout may have changed; do not attach a screenshot of "
                           "something else")
    page.wait_for_function(_BACKLOG_READY_JS, timeout=timeout_ms)

    result = {"engine": "", "series_shown": 0, "same_name_series": 0}
    if engine:
        matched = page.evaluate(_ISOLATE_ENGINE_JS, engine)
        if matched.get("error"):
            raise RuntimeError(f"could not isolate {engine!r} in the Backlog card: "
                               f"{matched['error']}")
        page.wait_for_timeout(1500)
        page.wait_for_function(_BACKLOG_READY_JS, timeout=timeout_ms)
        visible = page.evaluate(_LEGEND_STATE_JS)["visible"]
        # Verify the isolation took. Attaching a card that still shows every
        # engine, captioned as one engine, is exactly the convincing-but-wrong
        # evidence the panel-map rules exist to prevent.
        if len(visible) != 1 or visible[0].strip().lower() != engine.strip().lower():
            raise RuntimeError(
                f"isolating {engine!r} did not take effect — the card still shows "
                f"{len(visible)} series ({', '.join(visible[:4])}). Nothing was captured.")
        result.update(engine=visible[0], series_shown=len(visible),
                      same_name_series=matched.get("same_name_series", 1))

    # Getting a clean frame took three tries, so the order here matters:
    #   1. park the pointer — the legend click leaves an ApexCharts tooltip up;
    #   2. scroll the card clear of the page's STICKY filter bar. Hiding
    #      "overlapping fixed/sticky elements" alone missed it, because the chip
    #      showing through is a statically-positioned child of a sticky
    #      ancestor, and a computed-position test on the leaf says "static";
    #   3. hide whatever still overlaps;
    #   4. measure, then clip the viewport myself. locator.screenshot() scrolls
    #      the element into view again and undid step 2, yielding a frame offset
    #      by ~45px with the card title cut off.
    # Park the pointer (the legend click leaves an ApexCharts tooltip up), hide
    # anything floating over the card, then let Playwright clip the element.
    #
    # Two approaches were tried and rejected, both for reasons worth recording:
    # scrolling the card clear of the sticky filter bar with window.scrollBy()
    # does NOTHING here — this SPA scrolls an inner container, not the window,
    # so the card ended up below the fold and the clip height went negative. And
    # measuring the rect myself to page.screenshot(clip=...) raced Playwright's
    # own scroll-into-view. scrollIntoView (used by _FIND_BACKLOG_CARD_JS and by
    # locator.screenshot) is the only positioning that works, because it finds
    # the real scrolling ancestor.
    page.mouse.move(0, 0)
    page.evaluate(_HIDE_OVERLAPPING_OVERLAYS_JS)
    page.wait_for_timeout(250)
    card = page.locator(".ant-card").filter(
        has=page.locator(".ant-card-head-title", has_text="Backlog")).first
    out_path.parent.mkdir(parents=True, exist_ok=True)
    card.screenshot(path=str(out_path))
    return result


# Clicking ONE legend entry isolates that engine — see capture_backlog_card.
_ISOLATE_ENGINE_JS = """
(engine) => {
  const title = Array.from(document.querySelectorAll('.ant-card-head-title'))
    .find(el => /^backlog\\b/i.test((el.innerText || '').trim()));
  const card = title && title.closest('.ant-card');
  if (!card) return {error: 'no Backlog card'};
  const items = Array.from(card.querySelectorAll('.apexcharts-legend-series'));
  const named = items.filter(i => {
    const t = ((i.querySelector('.apexcharts-legend-text') || {}).textContent || '').trim();
    return t.toLowerCase() === engine.trim().toLowerCase();
  });
  if (!named.length) {
    const available = items.map(i =>
      ((i.querySelector('.apexcharts-legend-text') || {}).textContent || '').trim());
    return {error: 'no legend entry named ' + engine + '. Legend has: ' +
                   available.slice(0, 12).join(', ')};
  }
  named[0].click();
  return {same_name_series: named.length};
}
"""

_LEGEND_STATE_JS = """
() => {
  const title = Array.from(document.querySelectorAll('.ant-card-head-title'))
    .find(el => /^backlog\\b/i.test((el.innerText || '').trim()));
  const card = title.closest('.ant-card');
  const items = Array.from(card.querySelectorAll('.apexcharts-legend-series'));
  return {
    visible: items
      .filter(i => i.getAttribute('data:collapsed') !== 'true')
      .map(i => ((i.querySelector('.apexcharts-legend-text') || {}).textContent || '').trim()),
    total: items.length,
  };
}
"""

# Hide anything still floating over the card. Cosmetic only: the card itself is
# never modified, and nothing carrying data is touched.
_HIDE_OVERLAPPING_OVERLAYS_JS = """
() => {
  const title = Array.from(document.querySelectorAll('.ant-card-head-title'))
    .find(el => /^backlog\\b/i.test((el.innerText || '').trim()));
  const card = title && title.closest('.ant-card');
  if (!card) return 0;
  const target = card.getBoundingClientRect();
  const intersects = (r) => !(r.right < target.left || r.left > target.right ||
                              r.bottom < target.top || r.top > target.bottom);
  let hidden = 0;
  document.querySelectorAll('body *').forEach(el => {
    if (el === card || card.contains(el) || el.contains(card)) return;
    const cs = getComputedStyle(el);
    const floating = cs.position === 'fixed' || cs.position === 'sticky' ||
                     /apexcharts-tooltip/.test((el.className || '').toString());
    if (!floating || cs.visibility === 'hidden') return;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0 || !intersects(r)) return;
    el.style.visibility = 'hidden';
    hidden++;
  });
  return hidden;
}
"""

