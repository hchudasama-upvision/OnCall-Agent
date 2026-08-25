// Domain types shared across the pipeline. Mirrors the VictorOps incident-card
// fields observed in #alerts-devops and the SOP intake schema in DESIGN.md Appendix B.2.

export interface VictorOpsIncident {
  incidentNumber: number;
  organization: string;
  incidentName: string;
  entityDisplayName: string;
  monitoringTool: string;
  stateMessage: string;
  escalationPolicy: string;
  ackedBy?: string;
  slackPermalink: string;
  createdAt: string;
}

export type EvidenceKind = "screenshot" | "code-block" | "narrative" | "file";

export interface EvidenceItem {
  kind: EvidenceKind;
  title: string;
  /** Data URL, local file path, or plain text depending on `kind`. */
  content: string;
  caption?: string;
}

export type StepAuthority = "read-only" | "safe-action" | "human-judgment";

export interface EvidenceStep {
  description: string;
  authority: StepAuthority;
  /** How to gather this evidence: tool + query/click-path. */
  method: string;
  screenshotRequired: boolean;
  /** View/panel to capture when screenshotRequired is true. */
  screenshotTarget?: string;
  /** Text template to post alongside the evidence; supports {placeholders}. */
  postTemplate: string;
}

export interface DecisionPoint {
  question: string;
  branches: Array<{ criteria: string; outcome: string; agentMayDecide: boolean }>;
}

export interface SafeAction {
  name: string;
  parameters: Record<string, string>;
  preconditions: string[];
  postActionVerification: string;
}

export interface EscalationRule {
  mention: string;
  triggerCondition: string;
}

export interface KnownFalsePositive {
  pattern: string;
  disposition: string;
}

/** One curated SOP entry per Appendix B.2 — the registry's contract for a given alert type. */
export interface RunbookSop {
  alertType: string;
  fingerprintPatterns: RegExp[];
  defaultSeverity: "warning" | "critical";
  ackRule: string;
  evidenceSteps: EvidenceStep[];
  decisionPoints: DecisionPoint[];
  safeActions: SafeAction[];
  escalation: EscalationRule[];
  resolutionCriteria: string;
  knownFalsePositives: KnownFalsePositive[];
}

export interface IncidentEvidenceBundle {
  incident: VictorOpsIncident;
  sop: RunbookSop;
  items: EvidenceItem[];
  narrative: string;
  scopeOrg?: string;
  errorType?: string;
}
