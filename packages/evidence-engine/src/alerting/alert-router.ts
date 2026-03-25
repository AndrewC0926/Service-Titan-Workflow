import type { ControlDriftEvent } from '@compliance-engine/types';
import { logger } from '@compliance-engine/common';
import { postAlert, type Severity } from './slack.js';
import { createTicket, type JiraPriority } from './jira.js';

export interface AlertRouterConfig {
  slackWebhookUrl: string;
  slackChannelSecurity: string;
  slackChannelCompliance: string;
  jiraHost: string;
  jiraEmail: string;
  jiraApiToken: string;
}

/**
 * Route a drift event to Slack + Jira based on severity.
 *
 * Routing rules:
 *   PCI control failure → P1 + #security-alerts (critical)
 *   ISO27001/SOC2 Tier 1 → P2 + #compliance (high)
 *   All others → P3 + #compliance (medium)
 */
export async function handleDriftEvent(
  event: ControlDriftEvent,
  controlTier: number,
  config: AlertRouterConfig,
): Promise<{ jiraTicketId: string }> {
  const isPci = event.controlId.startsWith('PCIDSS');
  const isTier1 = controlTier === 1;

  let priority: JiraPriority;
  let severity: Severity;
  let channel: string;

  if (isPci && isTier1) {
    priority = 'P1';
    severity = 'critical';
    channel = config.slackChannelSecurity;
  } else if (isTier1) {
    priority = 'P2';
    severity = 'high';
    channel = config.slackChannelCompliance;
  } else {
    priority = 'P3';
    severity = 'medium';
    channel = config.slackChannelCompliance;
  }

  const message = `Control ${event.controlId} drifted: ${event.prevStatus} → ${event.newStatus}`;

  logger.info('Routing drift event', {
    controlId: event.controlId,
    priority,
    severity,
    channel,
  });

  // Post Slack alert and create Jira ticket in parallel
  const [, ticketId] = await Promise.all([
    postAlert(config.slackWebhookUrl, channel, message, severity),
    createTicket(
      {
        host: config.jiraHost,
        email: config.jiraEmail,
        apiToken: config.jiraApiToken,
      },
      event.controlId,
      event,
      priority,
    ),
  ]);

  return { jiraTicketId: ticketId };
}
