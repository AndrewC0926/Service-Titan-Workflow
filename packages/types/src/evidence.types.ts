export type Framework = 'SOC2' | 'ISO27001' | 'PCIDSS' | 'ISO42001';

export type ControlStatus = 'pass' | 'fail' | 'drift' | 'stale' | 'pending';

/**
 * Raw evidence collected from any source connector before normalization.
 */
export interface RawEvidence {
  source: string;
  collectedAt: Date;
  data: Record<string, unknown>;
  suggestedControlIds: string[];
  metadata: {
    apiVersion?: string;
    requestId?: string;
    rateLimitRemaining?: number;
    paginationToken?: string;
  };
}

/**
 * Normalized evidence mapped to a specific control in the control library.
 */
export interface ControlEvidence {
  id: string;
  controlId: string; // e.g. "SOC2-CC6.1"
  framework: Framework;
  status: ControlStatus;
  confidence: number; // 0.0–1.0
  evidence: RawEvidence;
  collectedAt: Date;
  expiresAt: Date;
  testOnceIds: string[]; // cross-framework duplicate control IDs
}
