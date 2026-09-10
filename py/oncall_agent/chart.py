#!/usr/bin/env python3
"""
A local chart renderer — points in, PNG out.

Lives at package level, not under AWS_Agent, because two different agents
needed it: CloudWatch will not render in GovCloud, and a Thanos query has no
dashboard to screenshot at all.

Why this exists
---------------
`cloudwatch get-metric-widget-image` renders graphs server-side and is the
preferred path — no browser, no settle-time problem. It does not work
everywhere: in the GovCloud account behind `us-1-gov` it fails every time with
"Throttling: Rate exceeded", including after three retries with backoff, while
the identical call against the commercial account returns a PNG immediately
(measured 2026-08-31). The metric DATA is fine there — get-metric-statistics
returns the real series — so the only thing missing is the picture.

Rather than post a throttling alert with no graph, this draws the series from
those same datapoints: inline SVG in a blank page, screenshotted with the
Playwright that is already a dependency. No plotting library, no CDN, nothing
fetched at render time.

It is labelled as what it is. The caption for a locally-drawn chart says so,
because a reader is entitled to know whether they are looking at CloudWatch's
own rendering or ours — and the numbers, not the picture, are the evidence
either way.
"""
import html
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from playwright.sync_api import sync_playwright

WIDTH, HEIGHT = 1100, 340
# top leaves room for the title AND the highest y-label, which collided at 46.
_MARGIN = {"left": 78, "right": 30, "top": 60, "bottom": 52}


def _nice_ceiling(value: float) -> float:
    """A round number at or above `value`, so the y-axis reads sensibly."""
    if value <= 0:
        return 1.0
    magnitude = 10 ** (len(str(int(value))) - 1)
    for step in (1, 2, 2.5, 5, 10):
        candidate = magnitude * step
        if candidate >= value:
            return candidate
    return magnitude * 10


def _format_value(value: float) -> str:
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= limit:
            trimmed = f"{value / limit:.1f}".rstrip("0").rstrip(".")
            return f"{trimmed}{suffix}"
    return f"{value:.0f}" if abs(value) >= 10 else f"{value:.2f}".rstrip("0").rstrip(".")


def build_svg(points: Sequence[Tuple[datetime, float]], title: str, subtitle: str = "",
              threshold: Optional[float] = None, unit: str = "") -> str:
    """One line chart as standalone SVG. No external anything."""
    plot_w = WIDTH - _MARGIN["left"] - _MARGIN["right"]
    plot_h = HEIGHT - _MARGIN["top"] - _MARGIN["bottom"]
    values = [v for _, v in points]
    top = _nice_ceiling(max(values + ([threshold] if threshold else []) + [1]))
    first, last = points[0][0], points[-1][0]
    span = max((last - first).total_seconds(), 1)

    def x_of(when: datetime) -> float:
        return _MARGIN["left"] + plot_w * ((when - first).total_seconds() / span)

    def y_of(value: float) -> float:
        return _MARGIN["top"] + plot_h * (1 - min(value / top, 1.0))

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
        f'viewBox="0 0 {WIDTH} {HEIGHT}" font-family="system-ui,-apple-system,Segoe UI,sans-serif">',
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="#ffffff"/>',
        f'<text x="18" y="26" font-size="15" font-weight="600" fill="#16191f">'
        f'{html.escape(title)}</text>',
    ]
    if subtitle:
        parts.append(f'<text x="18" y="42" font-size="11" fill="#687078">'
                     f'{html.escape(subtitle)}</text>')

    # y grid + labels
    for i in range(5):
        value = top * i / 4
        y = y_of(value)
        parts.append(f'<line x1="{_MARGIN["left"]}" y1="{y:.1f}" '
                     f'x2="{WIDTH - _MARGIN["right"]}" y2="{y:.1f}" '
                     f'stroke="#e9ebed" stroke-width="1"/>')
        parts.append(f'<text x="{_MARGIN["left"] - 8}" y="{y + 4:.1f}" font-size="11" '
                     f'fill="#687078" text-anchor="end">{_format_value(value)}</text>')

    # x labels: first, middle, last — enough to place the window, no clutter.
    # The outer two are anchored inward, or they clip at the frame edges.
    for when, anchor in ((first, "start"), (points[len(points) // 2][0], "middle"),
                         (last, "end")):
        parts.append(f'<text x="{x_of(when):.1f}" y="{HEIGHT - _MARGIN["bottom"] + 20}" '
                     f'font-size="11" fill="#687078" text-anchor="{anchor}">'
                     f'{when.strftime("%m-%d %H:%M")}</text>')

    if threshold:
        y = y_of(threshold)
        parts.append(f'<line x1="{_MARGIN["left"]}" y1="{y:.1f}" '
                     f'x2="{WIDTH - _MARGIN["right"]}" y2="{y:.1f}" stroke="#d13212" '
                     f'stroke-width="1.5" stroke-dasharray="6 4"/>')
        parts.append(f'<text x="{WIDTH - _MARGIN["right"] - 4}" y="{y - 6:.1f}" font-size="11" '
                     f'fill="#d13212" text-anchor="end">threshold '
                     f'{_format_value(threshold)}</text>')

    line = " ".join(f"{'M' if i == 0 else 'L'}{x_of(w):.1f},{y_of(v):.1f}"
                    for i, (w, v) in enumerate(points))
    area = (f'M{x_of(first):.1f},{y_of(0):.1f} '
            + " ".join(f"L{x_of(w):.1f},{y_of(v):.1f}" for w, v in points)
            + f' L{x_of(last):.1f},{y_of(0):.1f} Z')
    parts.append(f'<path d="{area}" fill="#0972d3" fill-opacity="0.10"/>')
    parts.append(f'<path d="{line}" fill="none" stroke="#0972d3" stroke-width="2"/>')

    # axes
    parts.append(f'<line x1="{_MARGIN["left"]}" y1="{_MARGIN["top"]}" '
                 f'x2="{_MARGIN["left"]}" y2="{HEIGHT - _MARGIN["bottom"]}" '
                 f'stroke="#aab7b8"/>')
    parts.append(f'<line x1="{_MARGIN["left"]}" y1="{HEIGHT - _MARGIN["bottom"]}" '
                 f'x2="{WIDTH - _MARGIN["right"]}" y2="{HEIGHT - _MARGIN["bottom"]}" '
                 f'stroke="#aab7b8"/>')
    peak = max(values)
    parts.append(f'<text x="{WIDTH - _MARGIN["right"]}" y="26" font-size="11" fill="#687078" '
                 f'text-anchor="end">peak {_format_value(peak)}'
                 f'{" " + html.escape(unit) if unit else ""} · '
                 f'{len(points)} datapoint(s)</text>')
    parts.append("</svg>")
    return "".join(parts)


def render_series_png(points: Sequence[Tuple[datetime, float]], title: str, out_path: Path,
                      subtitle: str = "", threshold: Optional[float] = None,
                      unit: str = "") -> Path:
    """Draw the series to a PNG. Raises if there is nothing to draw — an empty
    chart is worse than saying there was no data."""
    if not points:
        raise RuntimeError("no datapoints to draw — report 'no data' instead of an empty chart")
    svg = build_svg(sorted(points, key=lambda p: p[0]), title, subtitle, threshold, unit)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_context(viewport={"width": WIDTH, "height": HEIGHT}).new_page()
            page.set_content(f'<body style="margin:0">{svg}</body>', wait_until="load")
            page.locator("svg").screenshot(path=str(out_path))
        finally:
            browser.close()
    return out_path


def parse_datapoints(datapoints: List[dict], stat: str) -> List[Tuple[datetime, float]]:
    """CloudWatch get-metric-statistics Datapoints -> [(when, value)], sorted."""
    out: List[Tuple[datetime, float]] = []
    for point in datapoints or []:
        raw = point.get("Timestamp") or ""
        try:
            when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        value = point.get(stat)
        if isinstance(value, (int, float)):
            out.append((when.astimezone(timezone.utc), float(value)))
    return sorted(out, key=lambda p: p[0])
