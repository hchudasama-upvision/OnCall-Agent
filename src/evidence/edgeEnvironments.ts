/**
 * Environment registry for the real Edge UI/Controller API, keyed by the same
 * "aiw-xxx" token VictorOps/Alertmanager use in incident entity names (e.g.
 * "aiw-wpsc01"). Base URLs + auth scheme verified against the existing
 * `engine_task_stats.sh` helper script (DevOps repo) — GET requests to
 * `<baseUrl>/proc/tasks/stats/engines` with `Authorization: Bearer <token>`.
 *
 * Only environments whose .env URL uses the "processing.*" host pattern that
 * script confirms are included here. EDGE_STG198_URL, EDGE_PRD9_URL, and
 * EDGE_PRD8_URL use a different host ("edge-admin.*") that hasn't been
 * confirmed to serve the same API, so they're deliberately left out rather
 * than guessed.
 */

export interface EdgeEnvironment {
  key: string;
  baseUrl: string;
  token: string;
}

const ENV_VAR_MAP: Array<{ key: string; urlVar: string; tokenVar: string }> = [
  { key: "aiw-prod1001", urlVar: "EDGE_PROD1001_URL", tokenVar: "EDGE_PROD1001_TOKEN" },
  { key: "aiw-prd5001", urlVar: "EDGE_PRD5001_URL", tokenVar: "EDGE_PRD5001_TOKEN" },
  { key: "aiw-uk1001", urlVar: "EDGE_UKPROD_URL", tokenVar: "EDGE_UKPROD_TOKEN" },
  { key: "aiw-bmg1015", urlVar: "EDGE_BMG1015_URL", tokenVar: "EDGE_BMG1015_TOKEN" },
  { key: "aiw-zpfc02", urlVar: "EDGE_ZPFC02_URL", tokenVar: "EDGE_ZPFC02_TOKEN" },
  { key: "aiw-wpsc01", urlVar: "EDGE_GOV1_URL", tokenVar: "EDGE_GOV1_TOKEN" },
  { key: "aiw-dmh1001", urlVar: "EDGE_DMH_URL", tokenVar: "EDGE_DMH_TOKEN" },
  { key: "aiw-wpcc03", urlVar: "EDGE_CA1_URL", tokenVar: "EDGE_CA1_TOKEN" },
];

export function loadEdgeEnvironments(env: NodeJS.ProcessEnv = process.env): Record<string, EdgeEnvironment> {
  const environments: Record<string, EdgeEnvironment> = {};
  for (const { key, urlVar, tokenVar } of ENV_VAR_MAP) {
    const baseUrl = env[urlVar];
    const token = env[tokenVar];
    if (!baseUrl || !token) continue;
    environments[key] = { key, baseUrl: `${baseUrl.replace(/\/+$/, "")}/edge/v1`, token };
  }
  return environments;
}

/** Pulls the "aiw-xxx" environment key out of a VictorOps incident title/entity name. */
export function extractEnvironmentKey(text: string): string | undefined {
  const match = text.match(/\baiw-[a-z0-9]+\b/i);
  return match?.[0].toLowerCase();
}

/** The browser-facing site root (Edge UI SPA), as opposed to the `/edge/v1` JSON API base. */
export function toUiBaseUrl(env: EdgeEnvironment): string {
  return env.baseUrl.replace(/\/edge\/v1$/, "");
}
