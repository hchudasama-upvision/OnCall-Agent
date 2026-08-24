import path from "node:path";
import { mkdir } from "node:fs/promises";
import type { VictorOpsIncident } from "../types.js";
import type { EdgeUiClient } from "../evidence/edgeUiClient.js";
import { renderTaskSummaryHtml, renderFailedTaskListHtml } from "../evidence/edgeUiRenderer.js";
import { captureHtmlScreenshot } from "../evidence/screenshot.js";
import { composeEngineFailureThread } from "../slack/composer.js";
import type { SlackClient } from "../slack/client.js";
import { matchSop } from "../runbooks/registry.js";

export interface EngineFailurePipelineDeps {
  edgeUiClient: EdgeUiClient;
  slackClient: SlackClient;
  slackChannel: string;
  screenshotDir: string;
}

/**
 * End-to-end handling for the "Engine failure rate" alert type (DESIGN.md
 * Appendix B.3 item 1), reproducing the #119679 reference thread.
 */
export async function runEngineFailurePipeline(
  incident: VictorOpsIncident,
  deps: EngineFailurePipelineDeps,
): Promise<void> {
  const sop = matchSop(incident.incidentName);
  if (!sop || sop.alertType !== "engine-failure-rate") {
    throw new Error(
      `Incident #${incident.incidentNumber} does not match the engine-failure-rate fingerprint — refusing to run this pipeline.`,
    );
  }

  const evidence = await deps.edgeUiClient.getTaskEvidence(incident);

  await mkdir(deps.screenshotDir, { recursive: true });
  const taskSummaryPath = path.join(deps.screenshotDir, `${incident.incidentNumber}-task-summary.png`);
  const failedTasksPath = path.join(deps.screenshotDir, `${incident.incidentNumber}-failed-tasks.png`);

  await captureHtmlScreenshot(renderTaskSummaryHtml(evidence), taskSummaryPath);
  await captureHtmlScreenshot(renderFailedTaskListHtml(evidence), failedTasksPath);

  const thread = composeEngineFailureThread(incident, evidence, {
    taskSummary: taskSummaryPath,
    failedTasks: failedTasksPath,
  });

  await deps.slackClient.postThread(deps.slackChannel, thread);
}
