import type { RawEvidence, ControlEvidence } from './evidence.types.js';

/**
 * Every evidence connector must implement this interface.
 * Connectors pull raw evidence from source systems, normalize it
 * to the unified control library, and report health status.
 */
export interface Connector {
  /** Identifier for the source system (e.g. "github", "okta", "aws") */
  source: string;

  /** Cron expression defining the collection schedule */
  schedule: string;

  /** Pull raw evidence from the source system */
  collect(): Promise<RawEvidence[]>;

  /** Normalize raw evidence into control-mapped evidence */
  normalize(raw: RawEvidence[]): Promise<ControlEvidence[]>;

  /** Return true if the source system is reachable and credentials are valid */
  healthCheck(): Promise<boolean>;
}
