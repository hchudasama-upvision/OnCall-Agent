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
# Panels finish their queries after the page reports networkidle, and a
# screenshot taken mid-query is a picture of a spinner.
SETTLE_MS = int(os.environ.get("GRAFANA_SETTLE_MS", "2500"))


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
        page.wait_for_timeout(SETTLE_MS)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(out_path))
        return out_path
    finally:
        page.close()
