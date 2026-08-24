import type { EdgeEnvironment } from "./edgeEnvironments.js";

/**
 * Real Edge UI/Controller query, mirroring `engine_task_stats.sh`
 * (DevOps repo, cron on ops-monitoring1-ops): GET /proc/tasks/stats/engines
 * for a window, aggregated per engine per task status.
 */

export interface EngineTaskCounts {
  engineId: string;
  engineName: string;
  counts: Record<string, number>;
}

interface StatsEnginesResponse {
  counts?: Array<{ status: string; engineID: string; engineName: string; count?: number }>;
}

const NON_ACTIVE_STATUSES = new Set(["aborted", "scheduled", "rejected"]);

export async function fetchEngineTaskStats(
  env: EdgeEnvironment,
  startTimeEpochSeconds: number,
  endTimeEpochSeconds: number,
): Promise<EngineTaskCounts[]> {
  const url = `${env.baseUrl}/proc/tasks/stats/engines?startTime=${startTimeEpochSeconds}&endTime=${endTimeEpochSeconds}`;
  const res = await fetch(url, {
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${env.token}`,
    },
  });

  if (!res.ok) {
    throw new Error(`Edge UI stats query failed for ${env.key}: ${res.status} ${res.statusText}`);
  }

  const body = (await res.json()) as StatsEnginesResponse;
  const byEngine = new Map<string, EngineTaskCounts>();
  for (const row of body.counts ?? []) {
    if (!row.engineID || !row.status) continue;
    const existing = byEngine.get(row.engineID) ?? {
      engineId: row.engineID,
      engineName: row.engineName,
      counts: {},
    };
    existing.counts[row.status] = (existing.counts[row.status] ?? 0) + (row.count ?? 0);
    byEngine.set(row.engineID, existing);
  }
  return [...byEngine.values()];
}

/** Total active (non-aborted/scheduled/rejected) task count for an engine, per the script's tiering logic. */
export function activeTaskTotal(engine: EngineTaskCounts): number {
  return Object.entries(engine.counts)
    .filter(([status]) => !NON_ACTIVE_STATUSES.has(status))
    .reduce((sum, [, count]) => sum + count, 0);
}
