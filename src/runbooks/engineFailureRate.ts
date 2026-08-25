import type { RunbookSop } from "../types.js";

/**
 * SOP for "Engine failure rate above 15% / 100%" — Appendix B.2 intake, item 1
 * of B.3. Incident #119679 (Docling Chunk Engine) is the golden example this
 * SOP is written to reproduce; see DESIGN.md Appendix B.1 for the required
 * thread structure this pipeline must match.
 */
export const engineFailureRateSop: RunbookSop = {
  alertType: "engine-failure-rate",
  fingerprintPatterns: [/Engine failure rate (above 15%|100%)/i, /Engine.*failure rate.*\d+%/i],
  defaultSeverity: "critical",
  ackRule: "Ack immediately on match — no precondition.",
  evidenceSteps: [
    {
      description: "Identify the failing engine and its ID",
      authority: "read-only",
      method: "Edge UI Controller → Processing Tasks, filtered by the alert's engine name",
      screenshotRequired: false,
      postTemplate: "Engine: `{engineName}`\nEngine ID: `{engineId}`",
    },
    {
      description: "Quantify impact over the alert window",
      authority: "read-only",
      method: "Edge UI task summary panel (completed vs failed count/pct) for the window",
      screenshotRequired: true,
      screenshotTarget: "Processing Tasks summary panel",
      postTemplate:
        "Over the past {windowHours} hours, all {totalTasks} tasks processed by the {engineName} have failed, resulting in a {failedPct}% failure rate with {completedTasks} completed tasks.",
    },
    {
      description: "Determine scope — which organization(s) and error type are affected",
      authority: "read-only",
      method: "Edge UI failed-task list, grouped by organization and error type",
      screenshotRequired: true,
      screenshotTarget: "Failed tasks list filtered by organization",
      postTemplate:
        "All failures belong to the organization {scopeOrgName} (`{scopeOrgId}`) with same Error Type `{errorType}`.",
    },
    {
      description: "Pull raw error payload for a sample of failed tasks",
      authority: "read-only",
      method: "Job/task detail view → error log lines (code, message, source file/line)",
      screenshotRequired: false,
      postTemplate: "{errorLogBlock}",
    },
  ],
  decisionPoints: [
    {
      question: "Is the root cause in application/engine code, infra, or input data?",
      branches: [
        { criteria: "Error indicates a code-level failure (e.g. internal_error from engine logic)", outcome: "escalate to owning engineering team", agentMayDecide: false },
        { criteria: "Error indicates infra exhaustion (OOM, disk, connectivity)", outcome: "escalate to SRE/infra", agentMayDecide: false },
        { criteria: "Error indicates malformed/edge-case input data", outcome: "escalate to Data team", agentMayDecide: false },
      ],
    },
  ],
  safeActions: [],
  escalation: [
    {
      mention: "{owningTeamMention}",
      triggerCondition: "Root cause narrative implicates application code (per decision point above)",
    },
  ],
  resolutionCriteria:
    "Failure rate returns below 15% and holds for the alert's evaluation window (VictorOps auto-resolves the underlying alert).",
  knownFalsePositives: [],
};
