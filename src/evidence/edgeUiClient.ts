import { readFile } from "node:fs/promises";
import path from "node:path";
import type { VictorOpsIncident } from "../types.js";

export interface EdgeUiTaskEvidence {
  engineName: string;
  engineId: string;
  /** Human-readable alert window, e.g. "8 hours" or "15 minutes" — Edge UI reports both units. */
  windowLabel: string;
  totalTasks: number;
  completedTasks: number;
  failedTasks: number;
  completedPct: number;
  failedPct: number;
  scopeOrgName: string;
  scopeOrgId: string;
  errorType: string;
  errorLogLines: string[];
  /** Sample failed task/job pulled from the job-detail view, per DESIGN.md Appendix B.1 item 5. */
  sampleTaskId: string;
  sampleJobId: string;
  rootCauseNarrative: string;
  owningTeamMention: string;
}

export interface EdgeUiClient {
  getTaskEvidence(incident: VictorOpsIncident): Promise<EdgeUiTaskEvidence>;
}

/**
 * Fixture-backed client. Stands in for the real Edge UI client (now
 * LiveEdgeUiClient, see liveEdgeUiClient.ts) for incidents with no live
 * environment wired up yet, or for offline/test runs.
 */
export class FixtureEdgeUiClient implements EdgeUiClient {
  constructor(private readonly fixturesDir: string) {}

  async getTaskEvidence(incident: VictorOpsIncident): Promise<EdgeUiTaskEvidence> {
    const filePath = path.join(this.fixturesDir, `edge-ui-${incident.incidentNumber}.json`);
    const raw = await readFile(filePath, "utf-8");
    return JSON.parse(raw) as EdgeUiTaskEvidence;
  }
}
