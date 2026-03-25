import { z } from 'zod';
import { logger, validateEnv } from '@compliance-engine/common';
import { createDb } from '@compliance-engine/db';
import { controlEvidence as controlEvidenceTable } from '@compliance-engine/db';
import { buildConnectorRegistry } from './connectors/registry.js';

const cliEnvSchema = z.object({
  DATABASE_URL: z.string(),
  GITHUB_TOKEN: z.string(),
  GITHUB_ORG: z.string(),
  MAX_REPOS: z.coerce.number().int().positive().default(100),
});

async function main(): Promise<void> {
  const args = process.argv.slice(2);
  const sourceIdx = args.indexOf('--source');
  const source = sourceIdx !== -1 ? args[sourceIdx + 1] : undefined;

  if (!source) {
    throw new Error('Usage: connector:run --source=<connector-name>');
  }

  const env = validateEnv(cliEnvSchema);
  const db = createDb(env.DATABASE_URL);
  const registry = buildConnectorRegistry(process.env);
  const connector = registry.get(source);

  if (!connector) {
    throw new Error(`Unknown connector: ${source}. Available: ${[...registry.keys()].join(', ')}`);
  }

  logger.info(`Running health check for ${source}`);
  const healthy = await connector.healthCheck();
  if (!healthy) {
    throw new Error(`Health check failed for ${source}`);
  }

  logger.info(`Collecting evidence from ${source}`);
  const raw = await connector.collect();
  logger.info(`Collected ${raw.length} raw evidence items`);

  logger.info(`Normalizing evidence`);
  const normalized = await connector.normalize(raw);
  logger.info(`Produced ${normalized.length} control evidence items`);

  // Write to database
  for (const item of normalized) {
    await db.insert(controlEvidenceTable).values({
      controlId: item.controlId,
      source: item.evidence.source,
      status: item.status,
      confidence: String(item.confidence),
      rawData: item.evidence.data,
      collectedAt: item.collectedAt,
      expiresAt: item.expiresAt,
    });
  }

  logger.info(`Wrote ${normalized.length} evidence rows to database`);
}

main().catch((err: unknown) => {
  logger.error(`CLI failed: ${err instanceof Error ? err.message : String(err)}`);
  process.exit(1);
});
