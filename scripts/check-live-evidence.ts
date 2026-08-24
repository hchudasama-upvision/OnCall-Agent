import "dotenv/config";
import { LiveEdgeUiClient } from "../src/evidence/liveEdgeUiClient.js";
import { loadEdgeEnvironments } from "../src/evidence/edgeEnvironments.js";
import type { VictorOpsIncident } from "../src/types.js";

/**
 * Drives LiveEdgeUiClient against real prod1001 data without posting
 * anything or fabricating a VictorOps incident number/permalink — this is a
 * read-only check that the live composition (stats -> failed-task lookup ->
 * task detail -> org name) produces a sane EdgeUiTaskEvidence object.
 */
async function main() {
  const client = new LiveEdgeUiClient(loadEdgeEnvironments());

  const syntheticIncident: VictorOpsIncident = {
    incidentNumber: 0,
    organization: "wazee-digital-inc",
    incidentName: "[FIRING:1] aiw-prod1001 : SI2 Playback segment creator failure rate above 15%",
    entityDisplayName: "aiw-prod1001 : SI2 Playback segment creator",
    monitoringTool: "Alertmanager",
    stateMessage: "Engine has failure rate above 15% over the last 15 minutes.",
    escalationPolicy: "NOC-VT OnCall",
    slackPermalink: "(synthetic test input, not a real VictorOps incident)",
    createdAt: new Date().toISOString(),
  };

  const evidence = await client.getTaskEvidence(syntheticIncident);
  console.log(JSON.stringify(evidence, null, 2));
}

main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
