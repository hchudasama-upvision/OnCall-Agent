import os
import re
from dataclasses import dataclass
from typing import Dict, Optional

"""
Environment registry for the real Edge UI/Controller API, keyed by the same
"aiw-xxx" token VictorOps/Alertmanager use in incident entity names (e.g.
"aiw-wpsc01"). Base URLs + auth scheme verified against the existing
`engine_task_stats.sh` helper script (DevOps repo) — GET requests to
`<baseUrl>/proc/tasks/stats/engines` with `Authorization: Bearer <token>`.

Only environments whose .env URL uses the "processing.*" host pattern that
script confirms are included here. EDGE_STG198_URL, EDGE_PRD9_URL, and
EDGE_PRD8_URL use a different host ("edge-admin.*") that hasn't been
confirmed to serve the same API, so they're deliberately left out rather
than guessed.
"""


@dataclass
class EdgeEnvironment:
    key: str
    base_url: str
    token: str


_ENV_VAR_MAP = [
    ("aiw-prod1001", "EDGE_PROD1001_URL", "EDGE_PROD1001_TOKEN"),
    ("aiw-prd5001", "EDGE_PRD5001_URL", "EDGE_PRD5001_TOKEN"),
    ("aiw-uk1001", "EDGE_UKPROD_URL", "EDGE_UKPROD_TOKEN"),
    ("aiw-bmg1015", "EDGE_BMG1015_URL", "EDGE_BMG1015_TOKEN"),
    ("aiw-zpfc02", "EDGE_ZPFC02_URL", "EDGE_ZPFC02_TOKEN"),
    ("aiw-wpsc01", "EDGE_GOV1_URL", "EDGE_GOV1_TOKEN"),
    ("aiw-dmh1001", "EDGE_DMH_URL", "EDGE_DMH_TOKEN"),
    ("aiw-wpcc03", "EDGE_CA1_URL", "EDGE_CA1_TOKEN"),
]


def load_edge_environments(env: Optional[Dict[str, str]] = None) -> Dict[str, EdgeEnvironment]:
    env = env if env is not None else os.environ
    environments: Dict[str, EdgeEnvironment] = {}
    for key, url_var, token_var in _ENV_VAR_MAP:
        base_url = env.get(url_var)
        token = env.get(token_var)
        if not base_url or not token:
            continue
        environments[key] = EdgeEnvironment(key=key, base_url=base_url.rstrip("/") + "/edge/v1", token=token)
    return environments


def to_ui_base_url(env: EdgeEnvironment) -> str:
    """The browser-facing site root (Edge UI SPA), as opposed to the `/edge/v1` JSON API base."""
    return re.sub(r"/edge/v1$", "", env.base_url)
