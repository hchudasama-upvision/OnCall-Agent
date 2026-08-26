import os
import urllib.parse
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Optional

"""
Headless-browser capture of a Grafana panel — DESIGN.md §4.5 mechanism (b),
and on this Grafana the only mechanism that actually produces a picture.

Why this exists rather than just calling /render/: thanos-grafana.ops.
veritone.com (v12.1.0) has no grafana-image-renderer plugin installed
(confirmed via /api/plugins, 2026-08-24). Grafana does not fail loudly when
the plugin is missing — every /render/ URL returns HTTP 200 and a valid PNG
whose content reads "No image renderer available/installed". Attaching that
to an incident thread would look like evidence and be nothing, so
grafana.render_panel() now rejects it and callers fall back here.

Auth: the same service-account token, sent as an Authorization header on
every request the page makes (the app shell AND its data XHRs) via
Playwright's extra_http_headers. No browser login, no session cookie, no
user credential.

Requires Playwright's chromium and its OS libraries — the same dependency
the Edge UI screenshots in screenshot.py already have. If `playwright
install-deps` has not been run on this host, capture raises and the
investigation continues without graph panels rather than dying.
"""

DEFAULT_WIDTH = int(os.environ.get("GRAFANA_PANEL_WIDTH", "1000"))
DEFAULT_HEIGHT = int(os.environ.get("GRAFANA_PANEL_HEIGHT", "500"))
# Grafana's own theme param. Light reads better as a Slack thumbnail; set
# GRAFANA_THEME=dark to match what the on-call engineer sees in their browser.
THEME = os.environ.get("GRAFANA_THEME", "light")
# Panels finish their queries AFTER the page reports networkidle — confirmed
# the hard way: a fixed 2.5s settle produced a clean screenshot of the word
# "Loading …" for a timeseries panel while a gauge panel beside it rendered
# fine. So readiness is polled (see _PANEL_READY_JS) and this is only the
# final paint settle once content is actually on screen.
SETTLE_MS = int(os.environ.get("GRAFANA_SETTLE_MS", "800"))
# Upper bound on waiting for a panel to finish querying. Thanos queries over a
# multi-hour window are genuinely slow; better to wait than to post a spinner.
READY_TIMEOUT_MS = int(os.environ.get("GRAFANA_READY_TIMEOUT_MS", "30000"))

# "Has this panel actually drawn something?" Grafana renders a gauge/timeseries
# into <canvas> or <svg> and a table into <table>, so the presence of any of
# those plus the absence of the loading placeholder means the query returned.
# "No data" counts as READY (the query finished) but not as EVIDENCE — see
# _NO_DATA_JS below.
_PANEL_READY_JS = r"""
() => {
  const text = (document.body && document.body.innerText) || "";
  if (/Loading\s*(\u2026|\.\.\.)/i.test(text)) return false;
  if (/No data/i.test(text)) return true;
  if (/^\s*$/.test(text) === false && /error/i.test(text) && /panel/i.test(text)) return true;
  return !!document.querySelector("canvas, svg, table");
}
"""


# A panel whose template variable did not match anything renders a perfectly
# clean "No data" — indistinguishable, to a reader, from "this volume is idle".
# Posting that into an incident thread is the wrong-and-convincing failure this
# repo keeps guarding against, and it is the most likely symptom of a bad
# {label:N} index. Detected so the caller can drop it and say why.
_NO_DATA_JS = r"""
() => {
  // Match an element whose ENTIRE text is "No data" — not a substring of the
  // page. The canvas check this replaced was wrong: a gauge with no series
  // still draws its arc, so `!querySelector("canvas")` let it through, and a
  // gauge renders that empty arc in GREEN — an image that reads as "healthy"
  // while carrying no measurement at all. That is the single most misleading
  // thing this agent could attach to an incident.
  // Grafana has SEVERAL empty states and they do not look alike:
  //   "No data"  the query returned no series
  //   "N/A"      a template variable resolved to nothing (the common symptom
  //              of a variable this repo set to a value the dashboard cannot
  //              match — e.g. hostname on a panel that filters by instance)
  // Both render inside the normal panel chrome; a GAUGE draws its coloured
  // arc regardless, so the image reads as a healthy measurement either way.
  const EMPTY = ["no data", "n/a", "no data points"];
  const nodes = document.querySelectorAll("div, span, p");
  for (const el of nodes) {
    if (EMPTY.includes((el.textContent || "").trim().toLowerCase())) return true;
  }
  return false;
}
"""


class NoDataError(RuntimeError):
    """The panel rendered empty ("No data" / "N/A") — usually a wrong variable."""


def _base_url() -> str:
    url = os.environ.get("GRAFANA_URL", "").rstrip("/")
    if not url:
        raise RuntimeError("GRAFANA_URL is not set")
    return url


def _token() -> str:
    token = os.environ.get("GRAFANA_API_TOKEN") or os.environ.get("GRAFANA_TOKEN")
    if not token:
        raise RuntimeError("GRAFANA_API_TOKEN is not set")
    return token


def panel_url(dashboard_uid: str, panel_id: int, from_: str = "now-6h", to: str = "now",
              variables: Optional[Dict[str, str]] = None) -> str:
    params = {"panelId": panel_id, "from": from_, "to": to, "theme": THEME, "kiosk": ""}
    params.update(variables or {})
    return f"{_base_url()}/d-solo/{dashboard_uid}/_?{urllib.parse.urlencode(params)}"


@contextmanager
def grafana_browser(width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT):
    """One browser context for a batch of panels.

    Launching chromium costs about a second; a five-panel incident should pay
    that once, not five times.
    """
    from playwright.sync_api import sync_playwright

    headers = {"Authorization": f"Bearer {_token()}"}
    cf_id = os.environ.get("CF_ACCESS_CLIENT_ID")
    cf_secret = os.environ.get("CF_ACCESS_CLIENT_SECRET")
    if cf_id and cf_secret:
        headers["CF-Access-Client-Id"] = cf_id
        headers["CF-Access-Client-Secret"] = cf_secret

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            context = browser.new_context(
                viewport={"width": width, "height": height},
                device_scale_factor=2,          # readable axis labels in Slack
                extra_http_headers=headers,
            )
            yield context
        finally:
            browser.close()


def capture_panel(
    context,
    dashboard_uid: str,
    panel_id: int,
    out_path: Path,
    from_: str = "now-6h",
    to: str = "now",
    variables: Optional[Dict[str, str]] = None,
    timeout_ms: int = 45000,
) -> Path:
    """Screenshot one d-solo panel into out_path. Raises on a failed load."""
    page = context.new_page()
    try:
        page.goto(panel_url(dashboard_uid, panel_id, from_, to, variables),
                  wait_until="networkidle", timeout=timeout_ms)
        # A token that Grafana rejects lands on the login page, which
        # screenshots perfectly well and tells the reader nothing.
        if "/login" in page.url:
            raise RuntimeError(
                f"Grafana redirected to login for {dashboard_uid}:{panel_id} — the "
                f"service-account token was not accepted for browser page loads"
            )
        # Poll for real content rather than sleeping a guessed amount, and
        # fail rather than shoot: a screenshot of "Loading …" looks like
        # evidence in a thread and carries none. The caller drops this one
        # panel and keeps the rest.
        try:
            page.wait_for_function(_PANEL_READY_JS, timeout=READY_TIMEOUT_MS)
        except Exception:                           # noqa: BLE001 — PlaywrightTimeoutError
            raise RuntimeError(
                f"panel {dashboard_uid}:{panel_id} never finished loading within "
                f"{READY_TIMEOUT_MS}ms — refusing to attach a screenshot of the spinner "
                f"(raise GRAFANA_READY_TIMEOUT_MS if this query is legitimately slow)"
            )
        page.wait_for_timeout(SETTLE_MS)
        if page.evaluate(_NO_DATA_JS):
            variables_note = ", ".join(f"{k}={v}" for k, v in (variables or {}).items())
            raise NoDataError(
                f"panel {dashboard_uid}:{panel_id} rendered EMPTY (No data / N/A)"
                + (f" for {variables_note}" if variables_note else "")
                + " — most likely a template variable that matches nothing "
                  "(check the {label:N} indices in config/panel_map.json)"
            )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(out_path))
        return out_path
    finally:
        page.close()
