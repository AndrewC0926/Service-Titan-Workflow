import type { ControlEvidence } from '@compliance-engine/types';
import { logger } from '@compliance-engine/common';

interface AnecdotesConfig {
  apiKey: string;
  baseUrl: string;
}

/**
 * Client for pushing evidence to the Anecdotes GRC platform.
 * Implementation deferred to a later phase — this is the interface contract.
 */
export class AnecdotesClient {
  private readonly config: AnecdotesConfig;

  constructor(config: AnecdotesConfig) {
    this.config = config;
  }

  async pushEvidence(_evidence: ControlEvidence[]): Promise<void> {
    logger.info('AnecdotesClient.pushEvidence — not yet implemented', {
      baseUrl: this.config.baseUrl,
    });
  }

  async healthCheck(): Promise<boolean> {
    logger.info('AnecdotesClient.healthCheck — not yet implemented');
    return false;
  }
}
