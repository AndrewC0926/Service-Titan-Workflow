import type { Connector } from '@compliance-engine/types';
import { GitHubConnector } from './github.connector.js';

interface ConnectorEnv {
  GITHUB_TOKEN?: string;
  GITHUB_ORG?: string;
  MAX_REPOS?: string;
}

/**
 * Build the connector map from environment variables.
 * Only registers connectors whose required env vars are present.
 */
export function buildConnectorRegistry(env: ConnectorEnv): Map<string, Connector> {
  const registry = new Map<string, Connector>();

  if (env.GITHUB_TOKEN && env.GITHUB_ORG) {
    registry.set(
      'github',
      new GitHubConnector({
        token: env.GITHUB_TOKEN,
        org: env.GITHUB_ORG,
        maxRepos: Number(env.MAX_REPOS) || 100,
      }),
    );
  }

  return registry;
}
