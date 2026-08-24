import path from "node:path";
import { fileURLToPath } from "node:url";
import { readFile } from "node:fs/promises";
import "dotenv/config";
import { runEngineFailurePipeline } from "../src/pipeline/engineFailurePipeline.js";
import { FixtureEdgeUiClient } from "../src/evidence/edgeUiClient.js";
import { DryRunSlackClient, WebApiSlackClient, type SlackClient } from "../src/slack/client.js";
import type { VictorOpsIncident } from "../src/types.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const fixturesDir = path.join(__dirname, "..", "fixtures");
const screenshotDir = path.join(__dirname, "..", "dist", "evidence");

async function main() {
  const incident: VictorOpsIncident = JSON.parse(
    await readFile(path.join(fixturesDir, "incident-119679.json"), "utf-8"),
  );

  const slackClient: SlackClient =
    process.env.SLACK_BOT_TOKEN && process.env.SLACK_CHANNEL
      ? new WebApiSlackClient(process.env.SLACK_BOT_TOKEN)
      : new DryRunSlackClient();

  await runEngineFailurePipeline(incident, {
    edgeUiClient: new FixtureEdgeUiClient(fixturesDir),
    slackClient,
    slackChannel: process.env.SLACK_CHANNEL ?? "#comms-noc",
    screenshotDir,
  });

  console.log(`\nScreenshots written to ${screenshotDir}`);
}

main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
