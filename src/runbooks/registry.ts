import type { RunbookSop } from "../types.js";
import { engineFailureRateSop } from "./engineFailureRate.js";

/**
 * The runbook registry is the contract described in DESIGN.md §4 item 4: the
 * agent may only execute steps that exist here, tagged by authority. It never
 * improvises from free-text runbook prose at runtime. Additional SOPs (per
 * Appendix B.3) get added here as the NOC team supplies intake forms.
 */
const REGISTRY: RunbookSop[] = [engineFailureRateSop];

export function matchSop(incidentTitle: string): RunbookSop | undefined {
  return REGISTRY.find((sop) => sop.fingerprintPatterns.some((pattern) => pattern.test(incidentTitle)));
}
