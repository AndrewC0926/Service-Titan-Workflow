import type { ControlStatus } from './evidence.types.js';

/**
 * Drift event emitted when a control's status changes between collection runs.
 */
export interface ControlDriftEvent {
  id: string;
  controlId: string;
  prevStatus: ControlStatus;
  newStatus: ControlStatus;
  diffPayload: Record<string, unknown>;
  detectedAt: Date;
  resolvedAt?: Date;
  jiraTicketId?: string;
}
