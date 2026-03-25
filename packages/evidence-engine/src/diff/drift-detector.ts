import type { ControlEvidence, ControlDriftEvent } from '@compliance-engine/types';

/**
 * Compare current evidence against the previous snapshot for the same control.
 * Returns a drift event if the status changed, or null if unchanged.
 */
export function detectDrift(
  current: ControlEvidence,
  previous: ControlEvidence | undefined,
): ControlDriftEvent | null {
  if (!previous) return null;
  if (current.status === previous.status) return null;

  return {
    id: crypto.randomUUID(),
    controlId: current.controlId,
    prevStatus: previous.status,
    newStatus: current.status,
    diffPayload: {
      prevConfidence: previous.confidence,
      newConfidence: current.confidence,
      source: current.evidence.source,
    },
    detectedAt: new Date(),
  };
}
