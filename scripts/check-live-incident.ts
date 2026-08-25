import path from "node:path";
import { mkdir } from "node:fs/promises";
import "dotenv/config";
import { LiveEdgeUiClient } from "../src/evidence/liveEdgeUiClient.js";
import { loadEdgeEnvironments, extractEnvironmentKey, toUiBaseUrl } from "../src/evidence/edgeEnvironments.js";
import { withEdgeUiSession, captureFilteredEdgeUiView, downloadTaskAndJobLogs } from "../src/evidence/screenshot.js";
import { composeEngineFailureThread } from "../src/slack/composer.js";
import { DryRunSlackClient, WebApiSlackClient, type SlackClient } from "../src/slack/client.js";
import type { VictorOpsIncident } from "../src/types.js";

/**
 * Runs the full live pipeline (real Edge UI data, real screenshots, real
 * downloaded task/job logs) against a real VictorOps incident number/title,
 * and posts the composed thread to SLACK_CHANNEL. No fabricated Slack
 * permalink — pass the real one as a third argument if you have it,
 * otherwise the top-level post just omits the link.
 *
 * Usage: npm run check-live-incident -- 119709 "[FIRING:1] aiw-prd5001 : Engine failure rate above 15%" [permalink]
 */
async function main() {
  const [incidentNumberArg, incidentName, permalink] = process.argv.slice(2);
  if (!incidentNumberArg || !incidentName) {
    console.error('Usage: npm run check-live-incident -- <incidentNumber> "<incident title>" [slackPermalink]');
    process.exitCode = 1;
    return;
  }

  const screenshotDir = path.join(process.cwd(), "dist", "evidence");
  await mkdir(screenshotDir, { recursive: true });

  const incident: VictorOpsIncident = {
    incidentNumber: Number(incidentNumberArg),
    organization: "wazee-digital-inc",
    incidentName,
    entityDisplayName: incidentName,
    monitoringTool: "Alertmanager",
    stateMessage: "Engine failure rate above 15% over the last 15 minutes.",
    escalationPolicy: "NOC-VT OnCall",
    slackPermalink: permalink ?? "",
    createdAt: new Date().toISOString(),
  };

  const environments = loadEdgeEnvironments();
  const edgeUiClient = new LiveEdgeUiClient(environments);
  const evidence = await edgeUiClient.getTaskEvidence(incident);

  const envKey = extractEnvironmentKey(`${incident.incidentName} ${incident.entityDisplayName}`);
  const env = envKey ? environments[envKey] : undefined;
  if (!env) throw new Error(`No environment configured for ${envKey}`);
  if (!process.env.EDGE_USERNAME || !process.env.EDGE_PASSWORD) {
    throw new Error("EDGE_USERNAME/EDGE_PASSWORD not set — required for real Edge UI screenshots");
  }

  const tasksPagePath = path.join(screenshotDir, `incident-${incident.incidentNumber}-tasks-page.png`);
  const enginePagePath = path.join(screenshotDir, `incident-${incident.incidentNumber}-engine-page.png`);
  const uiBaseUrl = toUiBaseUrl(env);

  const { tasksWindowLabel, engineWindowLabel, stats, taskLogPath, jobLogPath, tdoId } = await withEdgeUiSession(
    uiBaseUrl,
    { username: process.env.EDGE_USERNAME, password: process.env.EDGE_PASSWORD },
    async (page) => {
      const tasksResult = await captureFilteredEdgeUiView(
        page,
        `${uiBaseUrl}/processing/tasks/`,
        evidence.engineName,
        15,
        tasksPagePath,
      );
      const engineResult = await captureFilteredEdgeUiView(
        page,
        `${uiBaseUrl}/processing/engine/`,
        evidence.engineName,
        15,
        enginePagePath,
      );
      const logs = await downloadTaskAndJobLogs(
        page,
        uiBaseUrl,
        evidence.sampleTaskId,
        evidence.sampleJobId,
        screenshotDir,
      );

      return {
        tasksWindowLabel: tasksResult.actualWindowLabel,
        engineWindowLabel: engineResult.actualWindowLabel,
        stats: tasksResult.stats,
        taskLogPath: logs.taskLogPath,
        jobLogPath: logs.jobLogPath,
        tdoId: logs.tdoId,
      };
    },
  );

  if (!stats) throw new Error("Could not scrape completed/failed stats off the Tasks page");

  // Same drift guard as the synthetic live test: the text must describe the
  // same window the screenshot actually shows.
  if (!tasksWindowLabel.startsWith("15")) {
    throw new Error(`Aborting: Tasks page window unexpectedly "${tasksWindowLabel}", not 15 minutes`);
  }
  if (!engineWindowLabel.startsWith("15")) {
    throw new Error(`Aborting: Engine page window unexpectedly "${engineWindowLabel}", not 15 minutes`);
  }

  const liveEvidence = { ...evidence, ...stats };

  const thread = composeEngineFailureThread(
    incident,
    liveEvidence,
    { taskSummary: tasksPagePath, failedTasks: enginePagePath },
    { tdoId, taskLogPath, jobLogPath },
  );

  const slackClient: SlackClient =
    process.env.SLACK_BOT_TOKEN && process.env.SLACK_CHANNEL
      ? new WebApiSlackClient(process.env.SLACK_BOT_TOKEN)
      : new DryRunSlackClient();

  await slackClient.postThread(process.env.SLACK_CHANNEL ?? "#comms-noc", thread);
  console.log(`Posted live check for incident #${incident.incidentNumber}.`);
}

main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
