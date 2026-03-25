import type { RawEvidence, ControlEvidence } from '@compliance-engine/types';

/**
 * Default evidence expiry: 24 hours from collection time.
 */
const DEFAULT_EXPIRY_HOURS = 24;

/**
 * Build a ControlEvidence record from a RawEvidence item.
 * Connectors call this to produce normalized evidence rows.
 */
export function buildControlEvidence(params: {
  controlId: string;
  framework: ControlEvidence['framework'];
  status: ControlEvidence['status'];
  confidence: number;
  raw: RawEvidence;
  testOnceIds?: string[];
  expiryHours?: number;
}): ControlEvidence {
  const expiryMs = (params.expiryHours ?? DEFAULT_EXPIRY_HOURS) * 60 * 60 * 1000;

  return {
    id: crypto.randomUUID(),
    controlId: params.controlId,
    framework: params.framework,
    status: params.status,
    confidence: params.confidence,
    evidence: params.raw,
    collectedAt: params.raw.collectedAt,
    expiresAt: new Date(params.raw.collectedAt.getTime() + expiryMs),
    testOnceIds: params.testOnceIds ?? [],
  };
}
