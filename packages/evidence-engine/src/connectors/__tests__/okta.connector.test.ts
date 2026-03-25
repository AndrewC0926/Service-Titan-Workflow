import nock from 'nock';
import { OktaConnector } from '../okta.connector.js';
import { AuthError } from '@compliance-engine/common';
import type { RawEvidence } from '@compliance-engine/types';

const TEST_DOMAIN = 'test.okta.com';
const TEST_TOKEN = 'test-okta-token';
const OKTA_BASE = `https://${TEST_DOMAIN}`;

function createConnector(): OktaConnector {
  return new OktaConnector({ domain: TEST_DOMAIN, token: TEST_TOKEN });
}

// -- Fixtures --

function userFixture(
  id: string,
  status: string,
  mfaEnrolled: boolean,
  isAdmin: boolean,
) {
  return {
    id,
    status,
    profile: { login: `${id}@test.com`, firstName: id, lastName: 'User' },
    _embedded: {
      factors: mfaEnrolled ? [{ factorType: 'push', status: 'ACTIVE' }] : [],
      roles: isAdmin ? [{ type: 'SUPER_ADMIN' }] : [],
    },
  };
}

function mfaPolicyFixture(status: string) {
  return {
    id: 'mfa-policy-1',
    name: 'MFA Enrollment Policy',
    status,
    type: 'MFA_ENROLL',
    conditions: {},
    settings: {
      factors: {
        okta_otp: { enroll: { self: 'REQUIRED' } },
      },
    },
  };
}

function sessionLogFixture(actor: string) {
  return {
    actor: { id: actor, displayName: actor },
    eventType: 'user.session.start',
    published: new Date().toISOString(),
    outcome: { result: 'SUCCESS' },
  };
}

// Mock all 3 Okta endpoints with sensible defaults.
// Individual tests can override specific endpoints before calling collect().
function mockOktaDefaults() {
  // Users page 1 (no pagination by default)
  nock(OKTA_BASE)
    .get('/api/v1/users')
    .query(true)
    .reply(200, [
      userFixture('u1', 'ACTIVE', true, false),
      userFixture('u2', 'ACTIVE', true, false),
    ]);

  // MFA policy
  nock(OKTA_BASE)
    .get('/api/v1/policies')
    .query({ type: 'MFA_ENROLL' })
    .reply(200, [mfaPolicyFixture('ACTIVE')]);

  // Session logs
  nock(OKTA_BASE)
    .get('/api/v1/logs')
    .query(true)
    .reply(200, [sessionLogFixture('u1')]);
}

beforeEach(() => {
  nock.cleanAll();
});

afterAll(() => {
  nock.restore();
});

describe('OktaConnector', () => {
  describe('collect()', () => {
    it('returns RawEvidence[] with correct suggestedControlIds', async () => {
      const connector = createConnector();
      mockOktaDefaults();

      const evidence = await connector.collect();

      expect(evidence.length).toBeGreaterThan(0);
      expect(evidence[0]!.source).toBe('okta');
      expect(evidence[0]!.collectedAt).toBeInstanceOf(Date);

      // Should contain all Okta-mapped control IDs
      const allControlIds = evidence.flatMap((e) => e.suggestedControlIds);
      expect(allControlIds).toEqual(
        expect.arrayContaining([
          'SOC2-CC6.1',
          'ISO27001-A.9.4.2',
          'PCIDSS-REQ8.3.2',
          'SOC2-CC6.2',
          'ISO27001-A.9.2.5',
          'SOC2-CC6.3',
          'ISO27001-A.9.2.3',
          'PCIDSS-REQ7.1',
          'PCIDSS-REQ8.3.9',
        ]),
      );
    });
  });

  describe('normalize()', () => {
    it('100% MFA enrollment → confidence 1.0, status pass', async () => {
      const connector = createConnector();
      const now = new Date();

      const raw: RawEvidence = {
        source: 'okta',
        collectedAt: now,
        suggestedControlIds: ['SOC2-CC6.1', 'ISO27001-A.9.4.2', 'PCIDSS-REQ8.3.2'],
        data: {
          users: [
            userFixture('u1', 'ACTIVE', true, false),
            userFixture('u2', 'ACTIVE', true, false),
          ],
          mfaPolicies: [mfaPolicyFixture('ACTIVE')],
          sessionLogs: [sessionLogFixture('u1')],
          mfaStats: { totalActive: 2, enrolled: 2, unenrolled: 0, enrollmentRate: 1.0 },
          inactiveUsers: [],
          adminUsers: [],
        },
        metadata: {},
      };

      const results = await connector.normalize([raw]);
      const mfaResult = results.find((r) => r.controlId === 'SOC2-CC6.1');

      expect(mfaResult).toBeDefined();
      expect(mfaResult!.confidence).toBe(1.0);
      expect(mfaResult!.status).toBe('pass');
    });

    it('0% MFA enrollment → confidence 0.0, status fail', async () => {
      const connector = createConnector();
      const now = new Date();

      const raw: RawEvidence = {
        source: 'okta',
        collectedAt: now,
        suggestedControlIds: ['SOC2-CC6.1', 'ISO27001-A.9.4.2', 'PCIDSS-REQ8.3.2'],
        data: {
          users: [
            userFixture('u1', 'ACTIVE', false, false),
            userFixture('u2', 'ACTIVE', false, false),
          ],
          mfaPolicies: [mfaPolicyFixture('INACTIVE')],
          sessionLogs: [],
          mfaStats: { totalActive: 2, enrolled: 0, unenrolled: 2, enrollmentRate: 0 },
          inactiveUsers: [],
          adminUsers: [],
        },
        metadata: {},
      };

      const results = await connector.normalize([raw]);
      const mfaResult = results.find((r) => r.controlId === 'SOC2-CC6.1');

      expect(mfaResult).toBeDefined();
      expect(mfaResult!.confidence).toBe(0.0);
      expect(mfaResult!.status).toBe('fail');
    });

    it('95% MFA enrollment → confidence 0.5, status drift', async () => {
      const connector = createConnector();
      const now = new Date();

      // 19 of 20 users enrolled = 95%
      const users = Array.from({ length: 20 }, (_, i) =>
        userFixture(`u${i}`, 'ACTIVE', i < 19, false),
      );

      const raw: RawEvidence = {
        source: 'okta',
        collectedAt: now,
        suggestedControlIds: ['SOC2-CC6.1', 'ISO27001-A.9.4.2', 'PCIDSS-REQ8.3.2'],
        data: {
          users,
          mfaPolicies: [mfaPolicyFixture('ACTIVE')],
          sessionLogs: [],
          mfaStats: { totalActive: 20, enrolled: 19, unenrolled: 1, enrollmentRate: 0.95 },
          inactiveUsers: [],
          adminUsers: [],
        },
        metadata: {},
      };

      const results = await connector.normalize([raw]);
      const mfaResult = results.find((r) => r.controlId === 'SOC2-CC6.1');

      expect(mfaResult).toBeDefined();
      expect(mfaResult!.confidence).toBe(0.5);
      expect(mfaResult!.status).toBe('drift');
    });
  });

  describe('pagination', () => {
    it('fetches multiple pages when next link is present', async () => {
      const connector = createConnector();

      // Page 1 with next link
      nock(OKTA_BASE)
        .get('/api/v1/users')
        .query(true)
        .reply(200, [userFixture('u1', 'ACTIVE', true, false)], {
          Link: `<${OKTA_BASE}/api/v1/users?after=cursor1&limit=200>; rel="next"`,
        });

      // Page 2 (no next link)
      nock(OKTA_BASE)
        .get('/api/v1/users')
        .query({ after: 'cursor1', limit: '200' })
        .reply(200, [userFixture('u2', 'ACTIVE', true, false)]);

      // MFA policy + session logs
      nock(OKTA_BASE)
        .get('/api/v1/policies')
        .query({ type: 'MFA_ENROLL' })
        .reply(200, [mfaPolicyFixture('ACTIVE')]);

      nock(OKTA_BASE)
        .get('/api/v1/logs')
        .query(true)
        .reply(200, []);

      const evidence = await connector.collect();
      const users = (evidence[0]!.data as Record<string, unknown>)['users'] as unknown[];
      expect(users).toHaveLength(2);
    });
  });

  describe('rate limiting', () => {
    it('retries after 429 using Retry-After header', async () => {
      const connector = createConnector();

      // First request: 429
      nock(OKTA_BASE)
        .get('/api/v1/users')
        .query(true)
        .reply(429, { errorCode: 'E0000047' }, {
          'Retry-After': '1',
        });

      // Retry: success
      nock(OKTA_BASE)
        .get('/api/v1/users')
        .query(true)
        .reply(200, [userFixture('u1', 'ACTIVE', true, false)]);

      // MFA policy + session logs
      nock(OKTA_BASE)
        .get('/api/v1/policies')
        .query({ type: 'MFA_ENROLL' })
        .reply(200, [mfaPolicyFixture('ACTIVE')]);

      nock(OKTA_BASE)
        .get('/api/v1/logs')
        .query(true)
        .reply(200, []);

      const evidence = await connector.collect();
      expect(evidence.length).toBeGreaterThan(0);
    }, 10000);
  });

  describe('auth failure', () => {
    it('throws AuthError immediately on 401, no retry', async () => {
      const connector = createConnector();

      // All 3 endpoints called in parallel — mock all with 401
      nock(OKTA_BASE)
        .get('/api/v1/users')
        .query(true)
        .reply(401, { errorCode: 'E0000011', errorSummary: 'Invalid token' });

      nock(OKTA_BASE)
        .get('/api/v1/policies')
        .query(true)
        .reply(401, { errorCode: 'E0000011' });

      nock(OKTA_BASE)
        .get('/api/v1/logs')
        .query(true)
        .reply(401, { errorCode: 'E0000011' });

      await expect(connector.collect()).rejects.toThrow(AuthError);
    });
  });

  describe('healthCheck()', () => {
    it('returns true when org endpoint returns 200', async () => {
      const connector = createConnector();

      nock(OKTA_BASE)
        .get('/api/v1/org')
        .reply(200, { id: 'org-1', name: 'Test Org' });

      const result = await connector.healthCheck();
      expect(result).toBe(true);
    });

    it('returns false (not throw) when org endpoint returns 500', async () => {
      const connector = createConnector();

      nock(OKTA_BASE)
        .get('/api/v1/org')
        .reply(500, { errorCode: 'E0000009' });

      const result = await connector.healthCheck();
      expect(result).toBe(false);
    });
  });
});
