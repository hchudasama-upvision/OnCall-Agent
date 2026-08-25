import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import grafana, grafana_capture

"""
Turns an alert fingerprint into rendered Grafana PNGs for the #comms-noc
thread — the "grafana pics" half of the evidence a NOC engineer attaches by
hand today.

Which panels get attached is decided HERE, by config/panel_map.json, not by
the model. That is deliberate and carried over from the noc-ai-lab
prototype: the LLM writes the narrative, deterministic code picks the
evidence, so a hallucinated dashboard uid can never become a posted
screenshot.

panel_map.json keys are written the way the alert reads in Slack, so they can
be compound ("VM ActiveRedAlarms / ActiveYellowAlarms (vmware_vcenter)")
while a fingerprint is one bare alertname. A raw dict lookup silently
attaches NO panels for those — hence _panel_index(), which normalizes keys
the same way case_library does for the case library.
"""

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "panel_map.json"

# How the PNGs are produced:
#   auto (default) — use Grafana's /render API when the image-renderer plugin
#                    is actually installed, otherwise drive a headless browser.
#   render         — force the /render API (fails loudly if there is no plugin).
#   browser        — force headless capture.
#   off            — attach no graph panels at all.
# The probe behind "auto" costs one HTTP call, so it is cached for the process.
CAPTURE_MODE = os.environ.get("GRAFANA_CAPTURE", "auto").lower()
_renderer_available: Optional[bool] = None


def capture_mode(log: Callable[[str], None] = print) -> str:
    """Resolve "auto" to the mechanism that will actually work here."""
    global _renderer_available
    if CAPTURE_MODE != "auto":
        return CAPTURE_MODE
    if _renderer_available is None:
        _renderer_available = grafana.renderer_available()
        log("Grafana image-renderer plugin " +
            ("found — using the /render API" if _renderer_available else
             "NOT installed — capturing panels with a headless browser instead"))
    return "render" if _renderer_available else "browser"


@dataclass
class PanelSpec:
    dashboard_uid: str
    panel_id: int
    from_: str
    to: str = "now"
    variables: Dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.dashboard_uid}:{self.panel_id}"


@dataclass
class RenderedPanel:
    spec: PanelSpec
    path: Path


def _split_variants(key: str) -> set:
    """Every alertname string an alert could contain for this mapping key."""
    variants = {key}
    for part in key.split("/"):
        part = re.sub(r"\([^)]*\)", "", part).strip()
        if len(part) >= 5:
            variants.add(part)
    return variants


class PanelMap:
    def __init__(self, config: dict):
        self.default_dashboard_uid: Optional[str] = config.get("dashboard_uid")
        self.default_from: str = config.get("default_from") or os.environ.get("PANEL_FROM", "now-6h")
        self._index: Dict[str, dict] = {}
        for key, entry in (config.get("panels_by_alertname") or {}).items():
            normalized = entry if isinstance(entry, dict) else {"panels": entry}
            for variant in _split_variants(key):
                self._index.setdefault(variant.lower(), normalized)

    def _suffix_match(self, key: str) -> Optional[dict]:
        """Match a mapping key that the alert name merely ENDS with.

        PandoLogic renders the host into the alertname — the line reads
        "PandoLogic - SQL41 DiskSpaceUtilizationWarning", so the parsed name is
        "SQL41 DiskSpaceUtilizationWarning" and an exact lookup on
        "DiskSpaceUtilizationWarning" misses. Anchored on a word boundary and
        longest-key-first, so "DiskSpaceUtilizationCritical" can never be
        satisfied by a mapping for "...Warning".
        """
        if not key:
            return None
        for candidate in sorted(self._index, key=len, reverse=True):
            if key == candidate or key.endswith(" " + candidate):
                return self._index[candidate]
        return None

    @staticmethod
    def _substitute(template: str, env_key: Optional[str], labels: Optional[List[str]],
                    hints: Optional[Dict[str, str]]) -> Optional[str]:
        """Fill {env}, {label:N} and {hint} in a template-variable value.

        {label:N} indexes the alert's label values (negative counts from the
        end) — explicit, checkable, and stable because Alertmanager renders a
        given alert type's labels in a fixed order. Returns None when a
        placeholder cannot be resolved, so the caller can drop the variable
        rather than send Grafana the literal string "{label:4}".
        """
        labels = labels or []
        hints = hints or {}
        out = template
        if "{env}" in out:
            if not env_key:
                return None
            out = out.replace("{env}", env_key)
        for index_text in re.findall(r"\{label:(-?\d+)\}", out):
            try:
                value = labels[int(index_text)]
            except (IndexError, ValueError):
                return None
            out = out.replace(f"{{label:{index_text}}}", value)
        for name in re.findall(r"\{(resolve:[a-z_]+|[a-z_]+)\}", out):
            if name not in hints:
                return None
            out = out.replace(f"{{{name}}}", hints[name])
        return out

    def _resolve_lookups(self, entry: dict, env_key, labels, hints) -> Dict[str, str]:
        """Run the entry's `resolve` block and return {name: value}.

        A failed lookup yields nothing, so the variable that depends on it is
        dropped and the panel is refused by the empty-panel guard — which is
        the right outcome: better no graph than one of the wrong host.
        """
        resolved: Dict[str, str] = {}
        for name, rule in (entry.get("resolve") or {}).items():
            promql = self._substitute(rule.get("query", ""), env_key, labels, hints)
            if not promql:
                continue
            try:
                value = grafana.resolve_label(
                    rule.get("datasource", "thanos-main-ds"), promql, rule.get("label", name))
            except Exception:                       # noqa: BLE001 — lookup is best-effort
                value = None
            if value:
                resolved[name] = value
        return resolved

    def specs_for(self, fingerprint: str, env_key: Optional[str] = None,
                  labels: Optional[List[str]] = None,
                  hints: Optional[Dict[str, str]] = None) -> List[PanelSpec]:
        key = (fingerprint or "").lower().strip()
        entry = self._index.get(key)
        if not entry:
            entry = self._suffix_match(key)
        if not entry:
            return []
        dashboard_uid = entry.get("dashboard_uid") or self.default_dashboard_uid
        if not dashboard_uid:
            # A panel list with nowhere to render it from is a config error
            # worth surfacing, not a silent no-evidence.
            raise RuntimeError(
                f'panel_map.json maps "{fingerprint}" to panels but no dashboard_uid is set '
                f"(neither on the entry nor at the top level)"
            )
        # Lookups first: a resolved value becomes just another hint, so
        # {resolve:instance} and {host} substitute through the same path.
        hints = dict(hints or {})
        for name, value in self._resolve_lookups(entry, env_key, labels, hints).items():
            hints[f"resolve:{name}"] = value

        variables: Dict[str, str] = {}
        for key, raw_value in (entry.get("variables") or {}).items():
            if not isinstance(raw_value, str):
                variables[key] = raw_value
                continue
            resolved = self._substitute(raw_value, env_key, labels, hints)
            if resolved is None:
                # Unresolvable variable: Grafana would fall back to the
                # dashboard default, i.e. someone else's host. Drop it and let
                # the panel render with its own default rather than a wrong
                # value dressed up as the alert's.
                continue
            variables[key] = resolved
        return [
            PanelSpec(
                dashboard_uid=dashboard_uid,
                panel_id=int(pid),
                from_=entry.get("from") or self.default_from,
                to=entry.get("to") or "now",
                variables=variables,
            )
            for pid in (entry.get("panels") or [])
        ]


def load_panel_map(path: Path = _CONFIG_PATH) -> PanelMap:
    if not path.exists():
        return PanelMap({})
    return PanelMap(json.loads(path.read_text()))


def _panel_path(out_dir: Path, spec: PanelSpec) -> Path:
    return out_dir / f"grafana-{spec.dashboard_uid}-panel{spec.panel_id}.png"


def render_panels(
    specs: List[PanelSpec],
    out_dir: Path,
    log: Callable[[str], None] = print,
) -> List[RenderedPanel]:
    """Produce a PNG per spec. One bad panel never means no evidence.

    Returns only the panels that came out; failures are logged and skipped.
    A panel costs several seconds either way, which is why callers run this
    alongside the model call rather than after it.
    """
    if not specs:
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    mode = capture_mode(log)
    if mode == "off":
        log("GRAFANA_CAPTURE=off — skipping graph panels")
        return []
    if mode == "browser":
        return _capture_via_browser(specs, out_dir, log)
    return _render_via_api(specs, out_dir, log)


def _render_via_api(specs, out_dir, log) -> List[RenderedPanel]:
    rendered: List[RenderedPanel] = []
    for spec in specs:
        try:
            png = grafana.render_panel(
                spec.dashboard_uid, spec.panel_id,
                from_=spec.from_, to=spec.to, variables=spec.variables,
            )
        except Exception as e:                      # noqa: BLE001 — operational, not a bug
            log(f"Panel {spec.label} failed to render: {str(e).splitlines()[0]}")
            continue
        path = _panel_path(out_dir, spec)
        path.write_bytes(png)
        rendered.append(RenderedPanel(spec=spec, path=path))
        log(f"Rendered panel {spec.label} ({len(png)} bytes) -> {path}")
    return rendered


def _capture_via_browser(specs, out_dir, log) -> List[RenderedPanel]:
    rendered: List[RenderedPanel] = []
    try:
        with grafana_capture.grafana_browser() as context:
            for spec in specs:
                path = _panel_path(out_dir, spec)
                try:
                    grafana_capture.capture_panel(
                        context, spec.dashboard_uid, spec.panel_id, path,
                        from_=spec.from_, to=spec.to, variables=spec.variables,
                    )
                except Exception as e:              # noqa: BLE001
                    log(f"Panel {spec.label} failed to capture: {str(e).splitlines()[0]}")
                    continue
                rendered.append(RenderedPanel(spec=spec, path=path))
                log(f"Captured panel {spec.label} ({path.stat().st_size} bytes) -> {path}")
    except Exception as e:                          # noqa: BLE001 — browser could not start at all
        log(f"Headless browser unavailable for Grafana capture: {str(e).splitlines()[0]} "
            f"(run `playwright install chromium` and `playwright install-deps`)")
    return rendered


def panel_caption(spec: PanelSpec) -> str:
    """Every image is captioned with source and time range, per DESIGN.md 4.5."""
    variables = " ".join(f"{k}={v}" for k, v in spec.variables.items())
    base = f"Grafana {spec.dashboard_uid} panel {spec.panel_id} · {spec.from_} → {spec.to}"
    return f"{base} · {variables}" if variables else base


def post_panels(
    client,
    channel: str,
    thread_ts: str,
    specs: List[PanelSpec],
    out_dir: Path,
    log: Callable[[str], None] = print,
) -> List[RenderedPanel]:
    """Render and upload evidence panels straight into an incident thread.

    Used by the triage path (which posts as it goes); the engine-failure
    pipeline instead collects RenderedPanel paths and hands them to
    decide_resolution as attachable evidence keys.
    """
    rendered = render_panels(specs, out_dir, log=log)
    for panel in rendered:
        client.files_upload_v2(
            channel=channel,
            thread_ts=thread_ts,
            file=str(panel.path),
            filename=panel.path.name,
            title=panel_caption(panel.spec),
            initial_comment=panel_caption(panel.spec),
        )
    if specs and not rendered:
        # Total failure is worth saying out loud — a thread with silently
        # missing screenshots reads as "the mapping is wrong" rather than
        # "Grafana is unreachable".
        client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=":warning: No Grafana evidence panels could be rendered "
                 f"({len(specs)} mapped). Check `python -m oncall_agent.grafana --check`.",
        )
    return rendered
