import * as https from 'node:https';
import type { ControlDriftEvent } from '@compliance-engine/types';
import { logger } from '@compliance-engine/common';

export type JiraPriority = 'P1' | 'P2' | 'P3';

export interface JiraConfig {
  host: string;
  email: string;
  apiToken: string;
}

const PRIORITY_MAP: Record<JiraPriority, string> = {
  P1: 'Highest',
  P2: 'High',
  P3: 'Low',
};

/**
 * Create a Jira ticket for a compliance drift event.
 * Returns the ticket key (e.g. "COMP-123").
 */
export async function createTicket(
  config: JiraConfig,
  controlId: string,
  driftEvent: ControlDriftEvent,
  priority: JiraPriority,
): Promise<string> {
  const payload = JSON.stringify({
    fields: {
      project: { key: 'COMP' },
      summary: `[${priority}] Compliance drift: ${controlId} changed from ${driftEvent.prevStatus} to ${driftEvent.newStatus}`,
      description: {
        type: 'doc',
        version: 1,
        content: [
          {
            type: 'paragraph',
            content: [
              {
                type: 'text',
                text: `Control ${controlId} drifted from "${driftEvent.prevStatus}" to "${driftEvent.newStatus}" at ${driftEvent.detectedAt.toISOString()}.`,
              },
            ],
          },
        ],
      },
      issuetype: { name: 'Bug' },
      priority: { name: PRIORITY_MAP[priority] },
      labels: ['compliance', 'drift', controlId],
    },
  });

  const auth = Buffer.from(`${config.email}:${config.apiToken}`).toString('base64');

  const response = await new Promise<{ status: number; body: string }>((resolve, reject) => {
    const req = https.request(
      {
        hostname: config.host,
        path: '/rest/api/3/issue',
        method: 'POST',
        headers: {
          Authorization: `Basic ${auth}`,
          'Content-Type': 'application/json',
          Accept: 'application/json',
          'User-Agent': 'compliance-engine',
        },
      },
      (res) => {
        const chunks: Buffer[] = [];
        res.on('data', (chunk: Buffer) => chunks.push(chunk));
        res.on('end', () => {
          resolve({
            status: res.statusCode ?? 0,
            body: Buffer.concat(chunks).toString('utf-8'),
          });
        });
      },
    );

    req.on('error', reject);
    req.write(payload);
    req.end();
  });

  if (response.status !== 201) {
    throw new Error(`Jira ticket creation failed: ${response.status} ${response.body}`);
  }

  const data = JSON.parse(response.body) as { key: string };
  logger.info('Jira ticket created', { ticketId: data.key, controlId, priority });
  return data.key;
}
