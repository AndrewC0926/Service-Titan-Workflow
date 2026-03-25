import { Queue, Worker, type Job } from 'bullmq';
import type { Connector } from '@compliance-engine/types';
import { logger } from '@compliance-engine/common';

const QUEUE_NAME = 'evidence-collection';

interface CollectionJobData {
  source: string;
}

/**
 * Create the BullMQ queue for evidence collection jobs.
 */
export function createCollectionQueue(redisUrl: string): Queue<CollectionJobData> {
  const url = new URL(redisUrl);
  return new Queue<CollectionJobData>(QUEUE_NAME, {
    connection: {
      host: url.hostname,
      port: Number(url.port) || 6379,
    },
  });
}

/**
 * Create a worker that processes evidence collection jobs by invoking
 * the matching connector's collect() and normalize() methods.
 */
export function createCollectionWorker(
  redisUrl: string,
  connectors: Map<string, Connector>,
): Worker<CollectionJobData> {
  const url = new URL(redisUrl);
  return new Worker<CollectionJobData>(
    QUEUE_NAME,
    async (job: Job<CollectionJobData>) => {
      const connector = connectors.get(job.data.source);
      if (!connector) {
        throw new Error(`Unknown connector source: ${job.data.source}`);
      }

      logger.info(`Collecting evidence from ${job.data.source}`);
      const raw = await connector.collect();

      logger.info(`Normalizing ${raw.length} evidence items from ${job.data.source}`);
      const normalized = await connector.normalize(raw);

      logger.info(`Collected ${normalized.length} control evidence items from ${job.data.source}`);
      return { count: normalized.length };
    },
    {
      connection: {
        host: url.hostname,
        port: Number(url.port) || 6379,
      },
    },
  );
}
