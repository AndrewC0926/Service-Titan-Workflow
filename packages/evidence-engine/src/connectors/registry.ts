import type { Connector } from '@compliance-engine/types';
import { GitHubConnector } from './github.connector.js';
import { OktaConnector } from './okta.connector.js';
import { AwsConnector } from './aws.connector.js';

interface ConnectorEnv {
  GITHUB_TOKEN?: string;
  GITHUB_ORG?: string;
  MAX_REPOS?: string;
  OKTA_DOMAIN?: string;
  OKTA_TOKEN?: string;
  AWS_ACCESS_KEY_ID?: string;
  AWS_SECRET_ACCESS_KEY?: string;
  AWS_REGION?: string;
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

  if (env.OKTA_DOMAIN && env.OKTA_TOKEN) {
    registry.set(
      'okta',
      new OktaConnector({
        domain: env.OKTA_DOMAIN,
        token: env.OKTA_TOKEN,
      }),
    );
  }

  if (env.AWS_ACCESS_KEY_ID && env.AWS_SECRET_ACCESS_KEY) {
    registry.set(
      'aws',
      new AwsConnector({
        region: env.AWS_REGION ?? 'us-east-1',
        accessKeyId: env.AWS_ACCESS_KEY_ID,
        secretAccessKey: env.AWS_SECRET_ACCESS_KEY,
      }),
    );
  }

  return registry;
}
