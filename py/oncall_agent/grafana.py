"""
Grafana evidence tool — server-side panel rendering, per DESIGN.md §4.5
mechanism (a): the preferred way to attach the graph screenshots NOC
engineers post by hand today.

Ported from the noc-ai-lab prototype (tools/grafana.py, 2026-08-23) and
adapted to this repo:
  - reads GRAFANA_API_TOKEN (the name this repo's .env already uses) and
    falls back to GRAFANA_TOKEN, which is what the prototype used, so an
    .env copied from either project works unchanged;
  - adds --list-dashboards / --list-panels, because config/panel_map.json
    has to be filled with REAL dashboard uids and panel ids off
    thanos-grafana.ops.veritone.com — those are discovered, never guessed.

Reads its OWN credentials from env — never from a prompt, never from code:

  GRAFANA_URL             https://thanos-grafana.ops.veritone.com (no trailing /)
  GRAFANA_API_TOKEN       Grafana service-account token (Viewer is enough)
  CF_ACCESS_CLIENT_ID     optional: Cloudflare Access service token id
  CF_ACCESS_CLIENT_SECRET optional: Cloudflare Access service token secret

Two ways to get evidence, in order of preference:

  render_panel() -> PNG, via /render/d-solo/... Needs the image-renderer
                    plugin installed server-side; many instances lack it.
  panel_data()   -> numbers, via /api/ds/query. Works on ANY Grafana, and is
                    the fallback when there is no renderer.

Self-test (run it while the VPN/WARP is up):
    python -m oncall_agent.grafana --check
    python -m oncall_agent.grafana --list-dashboards engine
    python -m oncall_agent.grafana --list-panels <dashboard_uid>
    python -m oncall_agent.grafana --render <dashboard_uid>:<panel_id> -o panel.png
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

TIMEOUT = 30


def is_configured(env=None) -> bool:
    """True when a render/query call could actually be attempted.

    Grafana is optional everywhere in this repo: without it the agent still
    gathers Edge UI evidence and still triages, it just attaches no graph
    panels. Callers gate on this rather than letting the first render raise
    SystemExit out of a handler thread.
    """
    env = env if env is not None else os.environ
    return bool(env.get("GRAFANA_URL") and (env.get("GRAFANA_API_TOKEN") or env.get("GRAFANA_TOKEN")))


def _config() -> tuple:
    url = os.environ.get("GRAFANA_URL", "").rstrip("/")
    # GRAFANA_API_TOKEN is this repo's name; GRAFANA_TOKEN was the prototype's.
    token = os.environ.get("GRAFANA_API_TOKEN") or os.environ.get("GRAFANA_TOKEN") or ""
    if not url or not token:
        raise SystemExit(
            "Set GRAFANA_URL and GRAFANA_API_TOKEN in .env first (see .env.example). "
            "Nothing is hardcoded here."
        )
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    # Only needed when the instance sits behind Cloudflare Access AND this
    # process is not already inside the tunnel (e.g. headless/cron runs).
    cf_id = os.environ.get("CF_ACCESS_CLIENT_ID")
    cf_secret = os.environ.get("CF_ACCESS_CLIENT_SECRET")
    if cf_id and cf_secret:
        headers["CF-Access-Client-Id"] = cf_id
        headers["CF-Access-Client-Secret"] = cf_secret
    return url, headers


def _request(path: str, method="GET", body=None, accept=None) -> tuple:
    """Returns (raw_bytes, content_type). Raises RuntimeError with a diagnosis."""
    url, headers = _config()
    if accept:
        headers["Accept"] = accept
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        detail = e.read()[:400].decode("utf-8", "replace")
        if e.code == 401:
            hint = ("token rejected, or Cloudflare Access blocked the request "
                    "(an HTML login page means you got Access, not Grafana)")
        elif e.code == 403:
            hint = ("token lacks permission — writes need Editor/Admin, "
                    "reads and renders need only Viewer")
        elif e.code == 404 and "/render/" in path:
            hint = "image-renderer plugin is not installed — use panel_data()"
        elif e.code == 404:
            hint = "no such dashboard/panel/endpoint"
        else:
            hint = "see body below"
        raise RuntimeError(f"{method} {path} -> HTTP {e.code} ({hint})\n{detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"{method} {path} -> cannot reach {url}: {e.reason}. "
            f"If this Grafana is behind a VPN/WARP, that tunnel must be up on "
            f"THIS machine, or supply CF_ACCESS_* service-token credentials."
        )


def health() -> dict:
    raw, _ = _request("/api/health")
    return json.loads(raw)


def whoami() -> dict:
    """Confirms the token works and shows what it is allowed to do."""
    raw, _ = _request("/api/user")
    return json.loads(raw)


def search_dashboards(query: str = "", limit: int = 50) -> list:
    q = urllib.parse.urlencode({"query": query, "type": "dash-db", "limit": limit})
    raw, _ = _request(f"/api/search?{q}")
    return json.loads(raw)


def dashboard_panels(dashboard_uid: str) -> list:
    """[(panel_id, title, type)] for a dashboard, rows flattened.

    This is how config/panel_map.json gets filled in: the alert-type ->
    panel-id mapping must come from the real dashboards, and a wrong id
    renders a real-looking PNG of the wrong graph, which is worse than no
    evidence at all.
    """
    raw, _ = _request(f"/api/dashboards/uid/{dashboard_uid}")
    dash = json.loads(raw).get("dashboard", {})
    out = []

    def walk(panels):
        for p in panels or []:
            if p.get("type") == "row":
                walk(p.get("panels"))
                continue
            out.append((p.get("id"), p.get("title", ""), p.get("type", "")))

    walk(dash.get("panels"))
    return out


def panel_data(datasource_uid: str, expr: str, from_="now-6h", to="now", step_seconds=60) -> dict:
    """Range query through Grafana (no renderer plugin needed)."""
    body = {
        "from": from_,
        "to": to,
        "queries": [{
            "refId": "A",
            "expr": expr,
            "datasource": {"uid": datasource_uid},
            "intervalMs": step_seconds * 1000,
            "maxDataPoints": 500,
        }],
    }
    raw, _ = _request("/api/ds/query", method="POST", body=body)
    return json.loads(raw)


def resolve_label(datasource_uid: str, promql: str, label: str) -> Optional[str]:
    """Run an instant query and return one label value from the first series.

    This exists because a dashboard variable is often NOT the thing the alert
    carries. "2. Windows Server Details" chains $job -> $hostname -> $instance,
    and its memory panel filters on $instance ("10.199.1.11:9182") while a
    Zabbix alert only names the host ("STG-SVC120"). Setting $hostname alone
    leaves $instance empty and the panel renders a green "N/A" gauge — a real
    reading, apparently, of nothing. Resolving the value against Prometheus
    turns a guess into a lookup.
    """
    query = urllib.parse.quote(promql)
    raw, _ = _request(f"/api/datasources/proxy/uid/{datasource_uid}/api/v1/query?query={query}")
    results = json.loads(raw).get("data", {}).get("result") or []
    for series in results:
        value = (series.get("metric") or {}).get(label)
        if value:
            return value
    return None


def renderer_available() -> bool:
    """Is the image-renderer plugin actually installed?

    This has to be asked of /api/plugins, NOT of /render/. A Grafana with no
    renderer answers every /render/ URL with HTTP 200 and a valid PNG that
    merely *says* "No image renderer available/installed" — verified against
    thanos-grafana.ops.veritone.com (v12.1.0) on 2026-08-24, where the
    prototype's probe-the-render-endpoint check reported a false PASS. Posting
    that placeholder into a live incident thread as evidence is the exact
    failure this function exists to prevent.
    """
    try:
        raw, _ = _request("/api/plugins?embedded=0")
    except (RuntimeError, SystemExit):
        return False
    return any("renderer" in (p.get("id") or "") for p in json.loads(raw))


def _png_size(png: bytes):
    """(width, height) from the PNG IHDR, or None if it is not a PNG."""
    if len(png) < 24 or png[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")


def render_panel(dashboard_uid: str, panel_id: int, from_="now-6h", to="now",
                 width=1000, height=500, tz="UTC", variables=None) -> bytes:
    """PNG of a single panel. Requires the image-renderer plugin server-side.

    `variables` are dashboard template variables (e.g. {"var-env":
    "aiw-prd5001"}); real NOC dashboards are almost always templated, so
    rendering without them gives whatever the default happens to be.
    """
    params = {"panelId": panel_id, "from": from_, "to": to,
              "width": width, "height": height, "tz": tz}
    params.update(variables or {})
    q = urllib.parse.urlencode(params)
    raw, ctype = _request(f"/render/d-solo/{dashboard_uid}/_?{q}", accept="image/png")
    if "image" not in ctype:
        raise RuntimeError(
            f"expected a PNG, got {ctype or 'unknown'} — the instance most "
            f"likely has no image renderer. Use capture_panel() or panel_data()."
        )
    # The renderer-missing placeholder comes back at its own fixed size, never
    # the size we asked for. Cheap, and it catches the case before the image
    # reaches Slack.
    size = _png_size(raw)
    if size and size != (width, height):
        raise RuntimeError(
            f"/render returned a {size[0]}x{size[1]} PNG instead of the requested "
            f"{width}x{height} — this is the 'No image renderer available/installed' "
            f"placeholder, not a panel. Use browser capture (GRAFANA_CAPTURE=browser)."
        )
    return raw


def bootstrap_token(name="oncall-agent") -> str:
    """Mint a service-account token using admin basic auth.

    Grafana cannot provision tokens from files, so a freshly started local
    stack needs this one step; everything afterwards uses the token exactly
    like production would. Reads GRAFANA_ADMIN_USER/PASSWORD.
    """
    url = os.environ.get("GRAFANA_URL", "http://localhost:3000").rstrip("/")
    user = os.environ.get("GRAFANA_ADMIN_USER", "admin")
    password = os.environ.get("GRAFANA_ADMIN_PASSWORD", "admin")
    basic = base64.b64encode(f"{user}:{password}".encode()).decode()

    def call(path, body=None, method="GET"):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url + path, data=data, method=method,
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/json",
                     "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{method} {path} -> HTTP {e.code}: "
                               f"{e.read()[:200].decode('utf-8', 'replace')}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"cannot reach {url}: {e.reason}")

    try:
        sa = call("/api/serviceaccounts", {"name": name, "role": "Admin", "isDisabled": False}, "POST")
        sa_id = sa["id"]
    except RuntimeError as e:
        # Re-running this is normal (the token is shown once, so a lost token
        # means minting another). Grafana 11 answered 409 here; Grafana 13
        # answers 400 with messageId serviceaccounts.ErrAlreadyExists.
        if "ErrAlreadyExists" not in str(e) and "409" not in str(e):
            raise
        hits = call(f"/api/serviceaccounts/search?query={name}")
        sa_id = next(a["id"] for a in hits["serviceAccounts"] if a["name"] == name)
    # Token names must be unique per service account, so a fixed name
    # collides on the second run. Find the lowest free suffix instead.
    used = {t["name"] for t in call(f"/api/serviceaccounts/{sa_id}/tokens")}
    n = 1
    while f"{name}-{sa_id}-{n}" in used:
        n += 1
    tok = call(f"/api/serviceaccounts/{sa_id}/tokens", {"name": f"{name}-{sa_id}-{n}"}, "POST")
    return tok["key"]


def _check() -> int:
    url = os.environ.get("GRAFANA_URL", "(unset)")
    print(f"target: {url}")
    try:
        h = health()
        print(f"  /api/health   OK   version={h.get('version')} db={h.get('database')}")
    except RuntimeError as e:
        print(f"  /api/health   FAIL {e}")
        return 1
    try:
        u = whoami()
        print(f"  /api/user     OK   login={u.get('login')} admin={u.get('isGrafanaAdmin')}")
    except RuntimeError as e:
        print(f"  /api/user     FAIL {e}")
        return 1
    try:
        raw, _ = _request("/api/datasources")
        ds = json.loads(raw)
        print(f"  datasources   OK   {len(ds)} visible: "
              + ", ".join(f"{d['name']}({d['type']}/{d['uid']})" for d in ds[:6]))
    except RuntimeError as e:
        print(f"  datasources   n/a  {e.args[0].splitlines()[0]}")
    if renderer_available():
        print("  /render       available — image-renderer plugin installed")
    else:
        print("  /render       NOT available — no image-renderer plugin; /render/ URLs "
              "return a 200 placeholder PNG, so panels must be captured with a headless "
              "browser instead (GRAFANA_CAPTURE=browser, the default fallback)")
    return 0


def _main(argv=None) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    p = argparse.ArgumentParser(prog="oncall_agent.grafana",
                                description="Grafana evidence tool (render panels, discover panel ids)")
    p.add_argument("--check", action="store_true",
                   help="probe reachability, token, datasources, renderer")
    p.add_argument("--bootstrap-token", action="store_true",
                   help="mint a service-account token via admin basic auth")
    p.add_argument("--list-dashboards", nargs="?", const="", metavar="QUERY",
                   help="search dashboards by title (fills config/panel_map.json)")
    p.add_argument("--list-panels", metavar="UID",
                   help="list panel ids + titles of one dashboard")
    p.add_argument("--render", metavar="UID:PANELID", help="render one panel to PNG")
    p.add_argument("-o", "--out", default="panel.png")
    p.add_argument("--from", dest="from_", default="now-6h")
    p.add_argument("--to", default="now")
    a = p.parse_args(argv)

    try:
        if a.bootstrap_token:
            key = bootstrap_token()
            print("Add these to .env (the token is shown only once):\n")
            print(f"GRAFANA_URL={os.environ.get('GRAFANA_URL', 'http://localhost:3000')}")
            print(f"GRAFANA_API_TOKEN={key}")
            return 0
        if a.check:
            return _check()
        if a.list_dashboards is not None:
            for d in search_dashboards(a.list_dashboards):
                print(f"{d.get('uid'):<24} {d.get('title')}")
            return 0
        if a.list_panels:
            for pid, title, kind in dashboard_panels(a.list_panels):
                print(f"{str(pid):>5}  {kind:<12} {title}")
            return 0
        if a.render:
            uid, _, pid = a.render.partition(":")
            png = render_panel(uid, int(pid), from_=a.from_, to=a.to)
            with open(a.out, "wb") as f:
                f.write(png)
            print(f"wrote {a.out} ({len(png)} bytes)")
            return 0
    except RuntimeError as e:      # no traceback for an operational problem
        print(f"grafana: {e}", file=sys.stderr)
        return 1
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(_main())
