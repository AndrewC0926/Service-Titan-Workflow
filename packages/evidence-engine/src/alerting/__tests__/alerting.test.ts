import nock from 'nock';
import { postAlert } from '../slack.js';
import { createTicket, type JiraConfig } from '../jira.js';
import { handleDriftEvent, type AlertRouterConfig } from '../alert-router.js';
import type { ControlDriftEvent } from '@compliance-engine/types';

const SLACK_WEBHOOK = 'https://hooks.slack.com/services/T00/B00/xxx';
const JIRA_HOST = 'test.atlassian.net';

const JIRA_CONFIG: JiraConfig = {
  host: JIRA_HOST,
  email: 'compliance@test.com',
  apiToken: 'test-token',
};

const ALERT_CONFIG: AlertRouterConfig = {
  slackWebhookUrl: SLACK_WEBHOOK,
  slackChannelSecurity: '#security-alerts',
  slackChannelCompliance: '#compliance',
  jiraHost: JIRA_HOST,
  jiraEmail: 'compliance@test.com',
  jiraApiToken: 'test-token',
};

function mockDriftEvent(controlId: string): ControlDriftEvent {
  return {
    id: 'drift-1',
    controlId,
    prevStatus: 'pass',
    newStatus: 'fail',
    diffPayload: { reason: 'Configuration changed' },
    detectedAt: new Date(),
  };
}

beforeEach(() => {
  nock.cleanAll();
});

afterAll(() => {
  nock.restore();
});

describe('Slack alerting', () => {
  it('posts alert with critical severity (red color)', async () => {
    nock('https://hooks.slack.com')
      .post('/services/T00/B00/xxx', (body: Record<string, unknown>) => {
        const attachments = body['attachments'] as Array<Record<string, unknown>>;
        return attachments?.[0]?.['color'] === '#dc3545'; // red
      })
      .reply(200, 'ok');

    await postAlert(SLACK_WEBHOOK, '#security-alerts', 'Critical control failure', 'critical');
    expect(nock.isDone()).toBe(true);
  });

  it('posts alert with medium severity (blue color)', async () => {
    nock('https://hooks.slack.com')
      .post('/services/T00/B00/xxx', (body: Record<string, unknown>) => {
        const attachments = body['attachments'] as Array<Record<string, unknown>>;
        return attachments?.[0]?.['color'] === '#0d6efd'; // blue
      })
      .reply(200, 'ok');

    await postAlert(SLACK_WEBHOOK, '#compliance', 'Medium control drift', 'medium');
    expect(nock.isDone()).toBe(true);
  });
});

describe('Jira ticket creation', () => {
  it('creates ticket with P1 priority', async () => {
    nock(`https://${JIRA_HOST}`)
      .post('/rest/api/3/issue', (body: Record<string, unknown>) => {
        const fields = body['fields'] as Record<string, unknown>;
        const priority = fields['priority'] as Record<string, unknown>;
        return priority?.['name'] === 'Highest';
      })
      .reply(201, { key: 'COMP-123' });

    const ticketId = await createTicket(
      JIRA_CONFIG,
      'PCIDSS-REQ1.2',
      mockDriftEvent('PCIDSS-REQ1.2'),
      'P1',
    );
    expect(ticketId).toBe('COMP-123');
  });

  it('creates ticket with P3 priority', async () => {
    nock(`https://${JIRA_HOST}`)
      .post('/rest/api/3/issue', (body: Record<string, unknown>) => {
        const fields = body['fields'] as Record<string, unknown>;
        const priority = fields['priority'] as Record<string, unknown>;
        return priority?.['name'] === 'Low';
      })
      .reply(201, { key: 'COMP-456' });

    const ticketId = await createTicket(
      JIRA_CONFIG,
      'SOC2-CC6.8',
      mockDriftEvent('SOC2-CC6.8'),
      'P3',
    );
    expect(ticketId).toBe('COMP-456');
  });
});

describe('Alert router', () => {
  it('routes PCI critical to P1 + #security-alerts', async () => {
    // Expect Slack post to security channel
    nock('https://hooks.slack.com')
      .post('/services/T00/B00/xxx')
      .reply(200, 'ok');

    // Expect Jira ticket creation
    nock(`https://${JIRA_HOST}`)
      .post('/rest/api/3/issue', (body: Record<string, unknown>) => {
        const fields = body['fields'] as Record<string, unknown>;
        const priority = fields['priority'] as Record<string, unknown>;
        return priority?.['name'] === 'Highest'; // P1
      })
      .reply(201, { key: 'COMP-789' });

    const result = await handleDriftEvent(
      mockDriftEvent('PCIDSS-REQ1.2'),
      1, // tier 1
      ALERT_CONFIG,
    );
    expect(result.jiraTicketId).toBe('COMP-789');
    expect(nock.isDone()).toBe(true);
  });

  it('routes SOC2 Tier 1 to P2 + #compliance', async () => {
    nock('https://hooks.slack.com')
      .post('/services/T00/B00/xxx')
      .reply(200, 'ok');

    nock(`https://${JIRA_HOST}`)
      .post('/rest/api/3/issue', (body: Record<string, unknown>) => {
        const fields = body['fields'] as Record<string, unknown>;
        const priority = fields['priority'] as Record<string, unknown>;
        return priority?.['name'] === 'High'; // P2
      })
      .reply(201, { key: 'COMP-101' });

    const result = await handleDriftEvent(
      mockDriftEvent('SOC2-CC6.1'),
      1, // tier 1
      ALERT_CONFIG,
    );
    expect(result.jiraTicketId).toBe('COMP-101');
    expect(nock.isDone()).toBe(true);
  });
});
