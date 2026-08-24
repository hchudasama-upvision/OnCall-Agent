import type { VictorOpsIncident } from "../types.js";
import type { EdgeUiClient, EdgeUiTaskEvidence } from "./edgeUiClient.js";
import type { EdgeEnvironment } from "./edgeEnvironments.js";
import { extractEnvironmentKey } from "./edgeEnvironments.js";
import { fetchEngineTaskStats, activeTaskTotal, type EngineTaskCounts } from "./engineTaskStats.js";
import { fetchTasksByStatus, fetchTaskDetail, fetchOrganizationName } from "./edgeApi.js";

function parseWindowMinutes(stateMessage: string): number | undefined {
  const match = stateMessage.match(/last\s+(\d+)\s*(hour|minute)s?/i);
  if (!match) return undefined;
  const amount = Number(match[1]);
  return match[2].toLowerCase() === "hour" ? amount * 60 : amount;
}

function formatWindowLabel(minutes: number): string {
  if (minutes % 60 === 0) {
    const hours = minutes / 60;
    return hours === 1 ? "1 hour" : `${hours} hours`;
  }
  return minutes === 1 ? "1 minute" : `${minutes} minutes`;
}

/** Picks the engine the incident text names, if any; otherwise the one with the most failures. */
function selectFailingEngine(engines: EngineTaskCounts[], incident: VictorOpsIncident): EngineTaskCounts | undefined {
  const haystack = `${incident.incidentName} ${incident.entityDisplayName} ${incident.stateMessage}`.toLowerCase();
  const named = engines.find((e) => e.engineName && haystack.includes(e.engineName.toLowerCase()));
  if (named && (named.counts.failed ?? 0) > 0) return named;

  return [...engines]
    .filter((e) => (e.counts.failed ?? 0) > 0)
    .sort((a, b) => (b.counts.failed ?? 0) - (a.counts.failed ?? 0))[0];
}

/**
 * DESIGN.md's decision point around root cause is tagged `agentMayDecide:
 * false` — a real narrative needs a human (or the downloaded task/job logs)
 * to actually read the failure and explain it, so this is left blank rather
 * than auto-generating a restatement of facts already in the raw error
 * block. `suggestOwningTeam` below is a best-effort draft for a human to
 * confirm, not an autonomous decision either.
 */
function suggestOwningTeam(errorType: string): string {
  const t = errorType.toLowerCase();
  if (t.includes("bad_data")) return "@data-team";
  if (t.includes("internal_error")) return "@engines-team";
  if (t.includes("oom") || t.includes("disk") || t.includes("timeout") || t.includes("connection")) return "@sre-team";
  return "@engines-team";
}

/**
 * Real Edge UI-backed client. Endpoints confirmed by capturing the logged-in
 * SPA's own network traffic (2026-08-21) — /proc/tasks/stats/engines,
 * /proc/tasks (status-filtered list), /proc/task/{id}/detail, and
 * /admin/organizations all accept the same static Bearer token already
 * verified for stats/engines.
 */
export class LiveEdgeUiClient implements EdgeUiClient {
  constructor(private readonly environments: Record<string, EdgeEnvironment>) {}

  async getTaskEvidence(incident: VictorOpsIncident): Promise<EdgeUiTaskEvidence> {
    const envKey = extractEnvironmentKey(`${incident.incidentName} ${incident.entityDisplayName}`);
    if (!envKey) {
      throw new Error(`Could not find an "aiw-xxx" environment key in incident #${incident.incidentNumber}'s title`);
    }
    const env = this.environments[envKey];
    if (!env) {
      throw new Error(`No confirmed Edge UI environment configured for "${envKey}" (incident #${incident.incidentNumber})`);
    }

    const windowMinutes = parseWindowMinutes(incident.stateMessage) ?? 15;
    const endTimeEpochSeconds = Math.floor(Date.now() / 1000);
    const startTimeEpochSeconds = endTimeEpochSeconds - windowMinutes * 60;

    const engineStats = await fetchEngineTaskStats(env, startTimeEpochSeconds, endTimeEpochSeconds);
    const engine = selectFailingEngine(engineStats, incident);
    if (!engine) {
      throw new Error(`No engine with failed tasks found in ${envKey} over the last ${windowMinutes} minutes`);
    }

    const totalTasks = activeTaskTotal(engine);
    const failedTasks = engine.counts.failed ?? 0;
    const completedTasks = engine.counts.complete ?? 0;
    const failedPct = totalTasks > 0 ? Math.round((failedTasks / totalTasks) * 100) : 0;
    const completedPct = totalTasks > 0 ? Math.round((completedTasks / totalTasks) * 100) : 0;

    const failedRecords = await fetchTasksByStatus(env, {
      startTimeEpochSeconds,
      endTimeEpochSeconds,
      status: "failed",
      limit: 100,
    });
    const sample = failedRecords
      .filter((r) => r.engineID === engine.engineId)
      .sort((a, b) => Date.parse(b.modifiedDateTime) - Date.parse(a.modifiedDateTime))[0];
    if (!sample) {
      throw new Error(`Engine ${engine.engineName} shows ${failedTasks} failed in stats but no matching task record was found`);
    }

    const detail = await fetchTaskDetail(env, sample.internalTaskID);
    const orgName = await fetchOrganizationName(env, detail.internalOrganizationID);

    return {
      engineName: engine.engineName,
      engineId: engine.engineId,
      windowLabel: formatWindowLabel(windowMinutes),
      totalTasks,
      completedTasks,
      failedTasks,
      completedPct,
      failedPct,
      scopeOrgName: orgName ?? `org ${detail.internalOrganizationID}`,
      scopeOrgId: detail.internalOrganizationID,
      errorType: detail.failureReason,
      errorLogLines: detail.failureDetail ? [detail.failureDetail] : [],
      sampleTaskId: detail.internalTaskID,
      sampleJobId: detail.internalJobID,
      rootCauseNarrative: "",
      owningTeamMention: suggestOwningTeam(detail.failureReason),
    };
  }
}
