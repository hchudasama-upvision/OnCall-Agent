import path from "node:path";
import { chromium, type Page } from "playwright";

/**
 * Captures a PNG screenshot of arbitrary HTML content via a headless browser.
 * This is mechanism (b) from DESIGN.md §4 item 5 (headless-browser capture of
 * Edge UI/Controller views). Mechanism (a) — Grafana's server-side render API
 * — needs no browser at all: it's a plain authenticated GET returning a PNG,
 * so it doesn't route through this function.
 */
export async function captureHtmlScreenshot(html: string, outPath: string): Promise<void> {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({ viewport: { width: 900, height: 100 } });
    await page.setContent(html, { waitUntil: "load" });
    await page.screenshot({ path: outPath, fullPage: true });
  } finally {
    await browser.close();
  }
}

/** For a real Edge UI/Controller URL once a read-only service account is provisioned. */
export async function captureUrlScreenshot(
  url: string,
  outPath: string,
  opts: { authHeader?: string } = {},
): Promise<void> {
  const browser = await chromium.launch();
  try {
    const context = await browser.newContext(
      opts.authHeader ? { extraHTTPHeaders: { Authorization: opts.authHeader } } : {},
    );
    const page = await context.newPage();
    await page.goto(url, { waitUntil: "networkidle" });
    await page.screenshot({ path: outPath, fullPage: true });
  } finally {
    await browser.close();
  }
}

/**
 * The Edge UI SPA authenticates its pages via a login-issued session cookie,
 * not the static Bearer token used by the JSON API (confirmed by testing —
 * header-only auth redirects to /auth/login/). Logs in once per call, then
 * screenshots the target page. `uiBaseUrl` is the site root (see
 * toUiBaseUrl), not the `/edge/v1` API base.
 */
export async function captureLoggedInScreenshot(
  uiBaseUrl: string,
  credentials: { username: string; password: string },
  pagePath: string,
  outPath: string,
): Promise<void> {
  await withEdgeUiSession(uiBaseUrl, credentials, async (page) => {
    await page.goto(`${uiBaseUrl}${pagePath}`, { waitUntil: "networkidle", timeout: 20000 });
    await page.screenshot({ path: outPath, fullPage: true });
  });
}

/**
 * Logs into the Edge UI SPA once and hands the authenticated page to `fn`,
 * so multiple screenshots can share a single login instead of one per call.
 * Uses `domcontentloaded` rather than `networkidle` for navigation waits —
 * this SPA polls continuously, so `networkidle` is unreliable here; explicit
 * element waits inside `fn` are the correct way to know the page is ready.
 */
export async function withEdgeUiSession<T>(
  uiBaseUrl: string,
  credentials: { username: string; password: string },
  fn: (page: Page) => Promise<T>,
): Promise<T> {
  const browser = await chromium.launch();
  try {
    const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
    const page = await context.newPage();
    await page.goto(`${uiBaseUrl}/auth/login/`, { waitUntil: "domcontentloaded", timeout: 20000 });
    await page.fill("#username", credentials.username);
    await page.fill("#password", credentials.password);
    await page.click('button[type="submit"]');
    // The click submits an async login request rather than a normal
    // navigation, so `domcontentloaded` right after it fires too early —
    // wait for the actual post-login redirect away from /auth/login/.
    await page.waitForURL((url) => !url.pathname.startsWith("/auth/login"), { timeout: 20000 });
    return await fn(page);
  } finally {
    await browser.close();
  }
}

export const MINUTE_WINDOW_PRESETS = [5, 10, 15, 30, 45];

/**
 * Finds the real time-range control. It's a plain `<button>`/`div[role=button]`
 * whose own text is exactly e.g. "15mins ago" — NOT an `.ant-select`, and
 * Playwright's `getByText`/`getByRole` locators don't reliably match it (a
 * framework quirk that cost a lot of debugging), so it's found by scanning
 * the DOM directly and clicked by coordinates instead of through a locator.
 * A naive `getByText(/ago$/i)` also risks matching unrelated "Xs ago"
 * relative-timestamp cells in the data table below, which is a real bug we
 * hit — this scan is restricted to button/[role=button] elements to avoid that.
 */
async function findTimeControlRect(page: Page): Promise<{ x: number; y: number; w: number; h: number } | undefined> {
  return page.evaluate(() => {
    let found: DOMRect | undefined;
    document.querySelectorAll("button, div[role='button']").forEach((el) => {
      const t = el.textContent?.trim() ?? "";
      if (/^\d+[a-z]*\s*ago$/i.test(t)) found = el.getBoundingClientRect();
    });
    return found ? { x: found.x, y: found.y, w: found.width, h: found.height } : undefined;
  });
}

async function readTimeControlLabel(page: Page): Promise<string | undefined> {
  return page.evaluate(() => {
    let label: string | undefined;
    document.querySelectorAll("button, div[role='button']").forEach((el) => {
      const t = el.textContent?.trim() ?? "";
      if (/^\d+[a-z]*\s*ago$/i.test(t)) label = t;
    });
    return label;
  });
}

export interface ScrapedTaskStats {
  completedTasks: number;
  completedPct: number;
  failedTasks: number;
  failedPct: number;
}

/**
 * Reads the "Completed tasks"/"Failed Tasks" stat cards straight off the
 * Tasks page — same numbers the screenshot shows. Pulling these separately
 * from a stats API call (as the original pipeline did) let the posted text
 * and the posted screenshot drift apart, since real task counts change
 * every few seconds; scraping the page itself guarantees they match.
 */
async function scrapeTaskStats(page: Page): Promise<ScrapedTaskStats | undefined> {
  // NOTE: this callback must not assign an inner arrow/function to a named
  // const — tsx/esbuild wraps named function bindings in a `__name(...)`
  // helper call that only exists in this module's scope, but Playwright
  // serializes the callback's source and re-runs it standalone in the
  // browser, so that helper is undefined there and the call throws. Inline
  // (anonymous) callbacks passed straight to .map/.forEach are unaffected.
  const [completed, failed] = await page.evaluate(() =>
    ["Completed tasks", "Failed Tasks"].map((label) => {
      let match: RegExpMatchArray | null = null;
      document.querySelectorAll("*").forEach((el) => {
        if (match) return;
        const t = el.textContent?.trim() ?? "";
        if (t !== label || el.children.length !== 0) return;
        let cur: Element | null = el;
        for (let i = 0; i < 10 && cur; i++) {
          const m = cur.textContent?.trim().match(new RegExp(`^(\\d+)${label}(\\d+)%$`));
          if (m) {
            match = m;
            break;
          }
          cur = cur.parentElement;
        }
      });
      return match ? { count: Number(match[1]), pct: Number(match[2]) } : undefined;
    }),
  );
  const raw = { completed, failed };

  if (!raw.completed || !raw.failed) return undefined;

  return {
    completedTasks: raw.completed.count,
    completedPct: raw.completed.pct,
    failedTasks: raw.failed.count,
    failedPct: raw.failed.pct,
  };
}

/**
 * Filters the Tasks or Engine dashboard to one engine + a relative minute
 * window (matching the filter controls you'd use by hand) and screenshots
 * the result. On views that group results behind a collapsible per-engine
 * section (e.g. /processing/engine/), expands that section and scrolls it
 * into view so the actual charts are captured, not the summary widget above.
 * Also scrapes the Tasks page's own completed/failed stat cards (undefined
 * on pages that don't have them, e.g. /processing/engine/) so callers can
 * report the exact numbers the screenshot shows instead of a separately
 * timed API call that can drift from what's pictured.
 */
export async function captureFilteredEdgeUiView(
  page: Page,
  pageUrl: string,
  engineName: string,
  windowMinutes: number,
  outPath: string,
): Promise<{ actualWindowLabel: string; stats?: ScrapedTaskStats }> {
  if (!MINUTE_WINDOW_PRESETS.includes(windowMinutes)) {
    throw new Error(
      `captureFilteredEdgeUiView only supports minute presets ${MINUTE_WINDOW_PRESETS.join(", ")} (got ${windowMinutes})`,
    );
  }

  await page.goto(pageUrl, { waitUntil: "domcontentloaded", timeout: 20000 });
  await page.waitForTimeout(800);

  const engineSelect = page.locator(".ant-select", { hasText: "Type to search engine" }).first();
  await engineSelect.waitFor({ state: "visible", timeout: 15000 });
  await engineSelect.click();
  await engineSelect.locator("input").fill(engineName);
  const option = page.locator(".ant-select-item-option-content", { hasText: engineName }).first();
  await option.waitFor({ state: "visible", timeout: 10000 });
  await option.click();
  await page.keyboard.press("Escape");
  await page.waitForTimeout(500);

  // Setting the window is occasionally flaky (animation-timing dependent —
  // the panel doesn't always register the click), so verify it actually
  // took and retry a few times rather than silently proceeding with the
  // wrong window.
  const expectedLabelPrefix = String(windowMinutes);
  let appliedLabel: string | undefined;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const timeControlRect = await findTimeControlRect(page);
      if (!timeControlRect) throw new Error(`Time-range control not found on ${pageUrl}`);
      await page.mouse.click(timeControlRect.x + timeControlRect.w / 2, timeControlRect.y + timeControlRect.h / 2);
      await page.waitForTimeout(800);
      // data-interval/data-type are the preset button's real attributes —
      // more stable than matching by visible name, which is ambiguous
      // ("1"/"2"/"3" appear in the hours AND days rows too).
      const minuteButton = page.locator(`button[data-interval="${windowMinutes}"][data-type="minutes"]`);
      await minuteButton.waitFor({ state: "visible", timeout: 5000 });
      await minuteButton.click({ force: true });
      await page.keyboard.press("Escape");
      await page.waitForTimeout(1500);

      appliedLabel = await readTimeControlLabel(page);
      if (appliedLabel?.startsWith(expectedLabelPrefix)) break;
    } catch {
      // The panel sometimes doesn't open on a given click (animation timing) —
      // fall through and retry rather than aborting on the first miss.
      await page.keyboard.press("Escape").catch(() => {});
    }
  }
  if (!appliedLabel?.startsWith(expectedLabelPrefix)) {
    throw new Error(`Could not set the ${windowMinutes}-minute window on ${pageUrl} after 3 attempts (stuck at "${appliedLabel}")`);
  }

  const collapseHeader = page.locator(".ant-collapse-header", { hasText: engineName }).first();
  if ((await collapseHeader.count()) > 0) {
    await collapseHeader.click();
    await page.waitForTimeout(1500);
    const content = page.locator(".ant-collapse-content").first();
    await content.evaluate((el) => el.scrollIntoView({ block: "end" }));
    await page.waitForTimeout(500);
  }

  const actualWindowLabel = await readTimeControlLabel(page);
  // The stat cards can lag a moment behind the window/engine filter update —
  // retry a few times rather than settling for `undefined` on first miss.
  let stats: ScrapedTaskStats | undefined;
  for (let attempt = 0; attempt < 4 && !stats; attempt++) {
    if (attempt > 0) await page.waitForTimeout(800);
    stats = await scrapeTaskStats(page);
  }

  // The mouse is still hovering the last-clicked control at this point,
  // which triggers a hover tooltip that would show up in the screenshot but
  // isn't part of the real page — move it away first.
  await page.mouse.move(10, 10);
  await page.waitForTimeout(300);
  await page.screenshot({ path: outPath });

  return { actualWindowLabel: actualWindowLabel ?? `${windowMinutes} minutes`, stats };
}

/**
 * Clicks Action -> "Download log" (never "Full download" — a different,
 * larger export that must not be triggered by this path) and saves the
 * resulting file. Assumes the page is already on the task/job detail view.
 */
async function downloadLogViaActionMenu(page: Page, outPath: string): Promise<void> {
  const actionButton = page.getByRole("button", { name: "Action" }).first();
  await actionButton.waitFor({ state: "visible", timeout: 10000 });
  await actionButton.click();
  await page.waitForTimeout(400);

  const downloadPromise = page.waitForEvent("download", { timeout: 15000 });
  await page.getByText("Download log", { exact: true }).click();
  const download = await downloadPromise;
  await download.saveAs(outPath);
}

/**
 * Reads the "TDO" field off a job-detail page. Handles two layouts seen in
 * practice: a single leaf with concatenated text ("TDO4280322462", no
 * separator) and a label/value table row where "TDO" is its own cell and the
 * numeric id sits in a sibling cell (confirmed via screenshot 2026-08-24).
 */
async function scrapeTdoId(page: Page): Promise<string | undefined> {
  return page.evaluate(() => {
    let tdo: string | undefined;
    document.querySelectorAll("*").forEach((el) => {
      if (tdo || el.children.length !== 0) return;
      const t = el.textContent?.trim() ?? "";
      const concatenated = t.match(/^TDO(\d+)$/);
      if (concatenated) {
        tdo = concatenated[1];
        return;
      }
      if (t !== "TDO") return;
      let row: Element | null = el.parentElement;
      for (let i = 0; i < 4 && row && !tdo; i++) {
        const leaves = Array.from(row.querySelectorAll("*")).filter((c) => c.children.length === 0);
        const valueCell = leaves.find((c) => c !== el && /^\d+$/.test(c.textContent?.trim() ?? ""));
        if (valueCell) tdo = valueCell.textContent!.trim();
        row = row.parentElement;
      }
    });
    return tdo;
  });
}

/**
 * Downloads the task's and job's log bundles (each a .zip containing engine
 * logs, ffmpeg logs, and IO configs) via the same "Download log" action a
 * human would click, and reads the job's TDO id off the job-detail page.
 * Only ever clicks "Download log" — "Full download" is a separate, larger
 * export this function must not trigger.
 */
export async function downloadTaskAndJobLogs(
  page: Page,
  uiBaseUrl: string,
  taskId: string,
  jobId: string,
  outDir: string,
): Promise<{ taskLogPath: string; jobLogPath: string; tdoId?: string }> {
  await page.goto(`${uiBaseUrl}/processing/tasks/detail/?taskId=${taskId}`, {
    waitUntil: "domcontentloaded",
    timeout: 20000,
  });
  await page.waitForTimeout(1200);
  const taskLogPath = path.join(outDir, `task-${taskId}.zip`);
  await downloadLogViaActionMenu(page, taskLogPath);

  await page.goto(`${uiBaseUrl}/processing/jobs/detail/?jobId=${jobId}`, {
    waitUntil: "domcontentloaded",
    timeout: 20000,
  });
  await page.waitForTimeout(1200);
  const tdoId = await scrapeTdoId(page);
  const jobLogPath = path.join(outDir, `job-${jobId}.zip`);
  await downloadLogViaActionMenu(page, jobLogPath);

  return { taskLogPath, jobLogPath, tdoId };
}
