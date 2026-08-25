import path from "node:path";
import { mkdir } from "node:fs/promises";
import "dotenv/config";
import { LiveEdgeUiClient } from "../src/evidence/liveEdgeUiClient.js";
import { loadEdgeEnvironments, extractEnvironmentKey, toUiBaseUrl } from "../src/evidence/edgeEnvironments.js";
import {
  withEdgeUiSession,
  captureFilteredEdgeUiView,
  downloadTaskAndJobLogs,
  MINUTE_WINDOW_PRESETS,
} from "../src/evidence/screenshot.js";
import { composeEngineFailureThread } from "../src/slack/composer.js";
import { DryRunSlackClient, WebApiSlackClient, type SlackClient } from "../src/slack/client.js";
import type { VictorOpsIncident } from "../src/types.js";

/**
 * Parses a real incident straight off the CLI so a different environment or
 * engine never requires a code edit — `extractEnvironmentKey` and
 * `selectFailingEngine` already pull both out of the incident's own text, so
 * this just needs to get that text in from argv instead of a hardcoded
 * object.
 *
 * Usage:
 *   npm run run-live-test -- "Incident #119875: [FIRING:1] aiw-prd5001 : Engine failure rate above 15%" "SI2 Playback segment creator" [windowMinutes] [slackPermalink]
 *
 * windowMinutes and slackPermalink are both optional and order-independent
 * after the first two args — a purely numeric trailing arg is taken as the
 * window, an "http..." trailing arg is taken as the real VictorOps/Slack
 * permalink (only supplied when you actually have one; never fabricated —
 * without it the top-level line falls back to plain, unlinked text).
 */
function parseIncidentFromArgs(argv: string[]): { incident: VictorOpsIncident; windowMinutes: number } {
  const [incidentLine, engineName, ...rest] = argv;
  if (!incidentLine || !engineName) {
    throw new Error(
      'Usage: npm run run-live-test -- "<incident line, e.g. Incident #119875: [FIRING:1] aiw-prd5001 : Engine failure rate above 15%>" "<engine name>" [windowMinutes] [slackPermalink]',
    );
  }
  const windowArg = rest.find((a) => /^\d+$/.test(a));
  const slackPermalink = rest.find((a) => /^https?:\/\//i.test(a)) ?? "";

  const numberMatch = incidentLine.match(/#(\d+)/);
  const incidentNumber = numberMatch ? Number(numberMatch[1]) : 0;
  const incidentName = incidentLine.replace(/^\s*Incident\s*#\d+:\s*/i, "").trim();
  const windowMinutes = windowArg ? Number(windowArg) : 15;
  if (!Number.isFinite(windowMinutes) || windowMinutes <= 0) {
    throw new Error(`Invalid windowMinutes argument: "${windowArg}"`);
  }

  const incident: VictorOpsIncident = {
    incidentNumber,
    organization: "wazee-digital-inc",
    incidentName,
    // Kept as internal matching text only (never rendered) — combining the
    // incident line + engine name here is what lets extractEnvironmentKey
    // and selectFailingEngine find both without any engine/env-specific code.
    entityDisplayName: `${incidentLine} ${engineName}`,
    monitoringTool: "Alertmanager",
    stateMessage: `Engine has failure rate above 15% over the last ${windowMinutes} minutes.`,
    escalationPolicy: "NOC-VT OnCall",
    slackPermalink,
    createdAt: new Date().toISOString(),
  };

  return { incident, windowMinutes };
}

/**
 * End-to-end live test: real Edge UI data (via LiveEdgeUiClient) AND real
 * Edge UI screenshots (logged-in browser capture, not our own rendered
 * charts) through the real composer, posted to the test channel.
 */
async function main() {
  const screenshotDir = path.join(process.cwd(), "dist", "evidence");
  await mkdir(screenshotDir, { recursive: true });

  const { incident, windowMinutes } = parseIncidentFromArgs(process.argv.slice(2));
  if (!MINUTE_WINDOW_PRESETS.includes(windowMinutes)) {
    throw new Error(
      `windowMinutes must be one of ${MINUTE_WINDOW_PRESETS.join(", ")} (Edge UI's own presets) — got ${windowMinutes}`,
    );
  }

  const environments = loadEdgeEnvironments();
  const edgeUiClient = new LiveEdgeUiClient(environments);
  const evidence = await edgeUiClient.getTaskEvidence(incident);

  const envKey = extractEnvironmentKey(`${incident.incidentName} ${incident.entityDisplayName}`);
  const env = envKey ? environments[envKey] : undefined;
  if (!env) throw new Error(`No environment configured for ${envKey}`);
  if (!process.env.EDGE_USERNAME || !process.env.EDGE_PASSWORD) {
    throw new Error("EDGE_USERNAME/EDGE_PASSWORD not set — required for real Edge UI screenshots");
  }

  const tasksPagePath = path.join(screenshotDir, "live-test-tasks-page.png");
  const enginePagePath = path.join(screenshotDir, "live-test-engine-page.png");
  const uiBaseUrl = toUiBaseUrl(env);

  const { tasksWindowLabel, engineWindowLabel, stats, taskLogPath, jobLogPath, tdoId } = await withEdgeUiSession(
    uiBaseUrl,
    { username: process.env.EDGE_USERNAME, password: process.env.EDGE_PASSWORD },
    async (page) => {
      const tasksResult = await captureFilteredEdgeUiView(
        page,
        `${uiBaseUrl}/processing/tasks/`,
        evidence.engineName,
        windowMinutes,
        tasksPagePath,
      );
      const engineResult = await captureFilteredEdgeUiView(
        page,
        `${uiBaseUrl}/processing/engine/`,
        evidence.engineName,
        windowMinutes,
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

  // Use the numbers scraped straight off the Tasks page screenshot, not the
  // separately-timed stats-API numbers in `evidence` — the two are fetched
  // several seconds apart and real task counts drift that fast, so quoting
  // the API numbers in the text next to a screenshot of different numbers
  // was the bug reported here.
  // Test-run override: ping a person, not the auto-suggested team, so this
  // stays visible during testing. Plain text, not a real `<@USERID>` mention
  // (that needs a users:read.email scope this token doesn't have) — won't
  // actually notify, just displays.
  const liveEvidence = { ...evidence, ...stats, owningTeamMention: "@Hardik Chudasama" };

  const thread = composeEngineFailureThread(
    incident,
    liveEvidence,
    { taskSummary: tasksPagePath, failedTasks: enginePagePath },
    { tdoId, taskLogPath, jobLogPath },
  );

  // Sanity check: both screenshots should now show the same window we set.
  // This must abort, not just warn — posting text that says "15 minutes"
  // next to a screenshot actually showing a 6-hour window is exactly the
  // drift bug this script exists to prevent.
  const expectedPrefix = String(windowMinutes);
  if (!tasksWindowLabel.startsWith(expectedPrefix)) {
    throw new Error(`Aborting: Tasks page window unexpectedly "${tasksWindowLabel}", not ${windowMinutes} minutes`);
  }
  if (!engineWindowLabel.startsWith(expectedPrefix)) {
    throw new Error(`Aborting: Engine page window unexpectedly "${engineWindowLabel}", not ${windowMinutes} minutes`);
  }

  const slackClient: SlackClient =
    process.env.SLACK_BOT_TOKEN && process.env.SLACK_CHANNEL
      ? new WebApiSlackClient(process.env.SLACK_BOT_TOKEN)
      : new DryRunSlackClient();

  await slackClient.postThread(process.env.SLACK_CHANNEL ?? "#comms-noc", thread);
  console.log("Posted live test thread.");
}

main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
