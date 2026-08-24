import type { VictorOpsIncident } from "../types.js";
import type { EdgeUiTaskEvidence } from "../evidence/edgeUiClient.js";

export interface SlackPost {
  text: string;
  /** Any file attachment — screenshots, but also downloaded log bundles (.zip). */
  filePaths?: string[];
}

export interface ComposedThread {
  topLevel: SlackPost;
  replies: SlackPost[];
}

function fillTemplate(template: string, evidence: EdgeUiTaskEvidence): string {
  return template.replace(/\{(\w+)\}/g, (_, key: string) => {
    const value = (evidence as unknown as Record<string, unknown>)[key];
    return value === undefined ? `{${key}}` : String(value);
  });
}


/**
 * Reproduces the #119679 reference thread structure (DESIGN.md Appendix B.1):
 * top-level alert post, then engine ID + quantified impact + screenshot,
 * scope + screenshot, raw error evidence, root-cause narrative, escalation.
 */
export function composeEngineFailureThread(
  incident: VictorOpsIncident,
  evidence: EdgeUiTaskEvidence,
  screenshotPaths: { taskSummary: string; failedTasks: string },
  logEvidence?: { tdoId?: string; taskLogPath: string; jobLogPath: string },
): ComposedThread {
  const incidentRef = incident.slackPermalink
    ? `<${incident.slackPermalink}|Incident #${incident.incidentNumber}>`
    : `Incident #${incident.incidentNumber}`;
  const topLevel: SlackPost = {
    text: `*Alert:*\n> ${incidentRef}: ${incident.incidentName}`,
  };

  const impactReply: SlackPost = {
    text: [
      `Engine: \`${evidence.engineName}\``,
      `Engine ID: \`${evidence.engineId}\``,
      "",
      fillTemplate("In the last {windowLabel}, {failedTasks} ({failedPct}%) tasks failed.", evidence),
    ].join("\n"),
    filePaths: [screenshotPaths.taskSummary],
  };

  const scopeReply: SlackPost = {
    text: fillTemplate("Traffic from: {scopeOrgName} (`{scopeOrgId}`)\nError Type: `{errorType}`", evidence),
    filePaths: [screenshotPaths.failedTasks],
  };

  const rawErrorReply: SlackPost = {
    text: "```" + evidence.errorLogLines.join("\n") + "```",
  };

  // A real root-cause narrative needs a human (or the downloaded logs) to
  // actually explain the failure — omit the line entirely rather than post
  // a restatement of the raw error when there's nothing more to say.
  const rootCauseReply: SlackPost = {
    text: [evidence.rootCauseNarrative, evidence.rootCauseNarrative ? "" : undefined, `${evidence.owningTeamMention} FYI^^`]
      .filter((line) => line !== undefined)
      .join("\n"),
  };

  const replies = [impactReply, scopeReply, rawErrorReply];

  if (logEvidence) {
    replies.push({
      text: logEvidence.tdoId ? `TDO: \`${logEvidence.tdoId}\`` : "TDO: (not found)",
      filePaths: [logEvidence.taskLogPath, logEvidence.jobLogPath],
    });
  }

  // The escalation @mention always comes last, after all evidence (including
  // the TDO/log files above) has been posted.
  replies.push(rootCauseReply);

  return { topLevel, replies };
}
