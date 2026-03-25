import * as https from 'node:https';
import { logger } from '@compliance-engine/common';

export type Severity = 'critical' | 'high' | 'medium';

const SEVERITY_COLORS: Record<Severity, string> = {
  critical: '#dc3545', // red
  high: '#ffc107',     // amber
  medium: '#0d6efd',   // blue
};

/**
 * Post an alert to a Slack channel via incoming webhook.
 */
export async function postAlert(
  webhookUrl: string,
  channel: string,
  message: string,
  severity: Severity,
): Promise<void> {
  const payload = JSON.stringify({
    channel,
    attachments: [
      {
        color: SEVERITY_COLORS[severity],
        text: message,
        fields: [
          { title: 'Severity', value: severity.toUpperCase(), short: true },
          { title: 'Channel', value: channel, short: true },
        ],
        ts: Math.floor(Date.now() / 1000),
      },
    ],
  });

  const url = new URL(webhookUrl);

  await new Promise<void>((resolve, reject) => {
    const req = https.request(
      {
        hostname: url.hostname,
        path: url.pathname,
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'User-Agent': 'compliance-engine',
        },
      },
      (res) => {
        res.resume(); // drain response
        res.on('end', () => {
          if ((res.statusCode ?? 0) >= 400) {
            reject(new Error(`Slack webhook returned ${res.statusCode}`));
          } else {
            resolve();
          }
        });
      },
    );

    req.on('error', reject);
    req.write(payload);
    req.end();
  });

  logger.info('Slack alert posted', { channel, severity });
}
