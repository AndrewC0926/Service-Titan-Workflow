import nock from 'nock';
import { GitHubConnector } from '../github.connector.js';
import { AuthError } from '@compliance-engine/common';
import type { RawEvidence } from '@compliance-engine/types';

const TEST_TOKEN = 'ghp_test123';
const TEST_ORG = 'test-org';

function createConnector(maxRepos = 100): GitHubConnector {
  return new GitHubConnector({
    token: TEST_TOKEN,
    org: TEST_ORG,
    maxRepos,
  });
}

// Helpers to build GitHub API response fixtures
function repoFixture(name: string, defaultBranch = 'main') {
  return { name, full_name: `${TEST_ORG}/${name}`, default_branch: defaultBranch };
}

function branchProtectionFixture(enforceAdmins = true, requiredReviews = true) {
  return {
    enforce_admins: { enabled: enforceAdmins },
    required_pull_request_reviews: requiredReviews
      ? { required_approving_review_count: 1 }
      : null,
  };
}

beforeEach(() => {
  nock.cleanAll();
});

afterAll(() => {
  nock.restore();
});

describe('GitHubConnector', () => {
  describe('collect()', () => {
    it('returns typed RawEvidence[] with correct suggestedControlIds', async () => {
      const connector = createConnector();

      // Mock: list repos (single page)
      nock('https://api.github.com')
        .get(`/orgs/${TEST_ORG}/repos`)
        .query(true)
        .reply(200, [repoFixture('repo-a')]);

      // Mock: branch protection
      nock('https://api.github.com')
        .get(`/repos/${TEST_ORG}/repo-a/branches/main/protection`)
        .reply(200, branchProtectionFixture(true, true));

      // Mock: secret scanning alerts
      nock('https://api.github.com')
        .get(`/repos/${TEST_ORG}/repo-a/secret-scanning/alerts`)
        .query(true)
        .reply(200, []);

      // Mock: CODEOWNERS check
      nock('https://api.github.com')
        .get(`/repos/${TEST_ORG}/repo-a/contents/CODEOWNERS`)
        .reply(200, { name: 'CODEOWNERS' });

      const evidence = await connector.collect();

      expect(evidence.length).toBeGreaterThan(0);
      expect(evidence[0]).toMatchObject({
        source: 'github',
        suggestedControlIds: expect.arrayContaining([
          'SOC2-CC8.1',
          'ISO27001-A.14.2.2',
          'PCIDSS-REQ6.3',
        ]),
      });
      expect(evidence[0]!.collectedAt).toBeInstanceOf(Date);
      expect(evidence[0]!.data).toBeDefined();
    });
  });

  describe('normalize()', () => {
    it('calculates confidence correctly for all 3 tiers', async () => {
      const connector = createConnector();
      const now = new Date();

      // Full protection = confidence 1.0
      const fullProtection: RawEvidence = {
        source: 'github',
        collectedAt: now,
        suggestedControlIds: ['SOC2-CC8.1', 'ISO27001-A.14.2.2', 'PCIDSS-REQ6.3'],
        data: {
          repos: [
            {
              name: 'repo-a',
              defaultBranch: 'main',
              branchProtection: {
                enabled: true,
                enforceAdmins: true,
                requiredReviews: true,
                requiredApprovingCount: 1,
              },
              secretAlerts: [],
              hasCodeowners: true,
            },
          ],
          totalRepos: 1,
          reposChecked: 1,
        },
        metadata: {},
      };

      // Partial protection = confidence 0.5
      const partialProtection: RawEvidence = {
        source: 'github',
        collectedAt: now,
        suggestedControlIds: ['SOC2-CC8.1', 'ISO27001-A.14.2.2', 'PCIDSS-REQ6.3'],
        data: {
          repos: [
            {
              name: 'repo-a',
              defaultBranch: 'main',
              branchProtection: {
                enabled: true,
                enforceAdmins: false,
                requiredReviews: false,
                requiredApprovingCount: 0,
              },
              secretAlerts: [],
              hasCodeowners: false,
            },
          ],
          totalRepos: 1,
          reposChecked: 1,
        },
        metadata: {},
      };

      // No protection = confidence 0.0
      const noProtection: RawEvidence = {
        source: 'github',
        collectedAt: now,
        suggestedControlIds: ['SOC2-CC8.1', 'ISO27001-A.14.2.2', 'PCIDSS-REQ6.3'],
        data: {
          repos: [
            {
              name: 'repo-a',
              defaultBranch: 'main',
              branchProtection: {
                enabled: false,
                enforceAdmins: false,
                requiredReviews: false,
                requiredApprovingCount: 0,
              },
              secretAlerts: [],
              hasCodeowners: false,
            },
          ],
          totalRepos: 1,
          reposChecked: 1,
        },
        metadata: {},
      };

      const fullResult = await connector.normalize([fullProtection]);
      const partialResult = await connector.normalize([partialProtection]);
      const noResult = await connector.normalize([noProtection]);

      expect(fullResult[0]!.confidence).toBe(1.0);
      expect(fullResult[0]!.status).toBe('pass');

      expect(partialResult[0]!.confidence).toBe(0.5);
      expect(partialResult[0]!.status).toBe('fail');

      expect(noResult[0]!.confidence).toBe(0.0);
      expect(noResult[0]!.status).toBe('fail');
    });
  });

  describe('pagination', () => {
    it('fetches multiple pages when Link header is present', async () => {
      const connector = createConnector();

      // Page 1 with Link header pointing to page 2
      nock('https://api.github.com')
        .get(`/orgs/${TEST_ORG}/repos`)
        .query(true)
        .reply(200, [repoFixture('repo-a')], {
          Link: '<https://api.github.com/orgs/test-org/repos?page=2&per_page=100>; rel="next"',
        });

      // Page 2 (no Link header = last page)
      nock('https://api.github.com')
        .get(`/orgs/${TEST_ORG}/repos`)
        .query({ page: '2', per_page: '100' })
        .reply(200, [repoFixture('repo-b')]);

      // Branch protection + secret scanning + CODEOWNERS for both repos
      for (const repo of ['repo-a', 'repo-b']) {
        nock('https://api.github.com')
          .get(`/repos/${TEST_ORG}/${repo}/branches/main/protection`)
          .reply(200, branchProtectionFixture());

        nock('https://api.github.com')
          .get(`/repos/${TEST_ORG}/${repo}/secret-scanning/alerts`)
          .query(true)
          .reply(200, []);

        nock('https://api.github.com')
          .get(`/repos/${TEST_ORG}/${repo}/contents/CODEOWNERS`)
          .reply(200, { name: 'CODEOWNERS' });
      }

      const evidence = await connector.collect();
      const repos = (evidence[0]!.data as Record<string, unknown>)['repos'] as unknown[];
      expect(repos).toHaveLength(2);
    });
  });

  describe('rate limiting', () => {
    it('retries after 429 using X-RateLimit-Reset header', async () => {
      const connector = createConnector();
      const resetTime = Math.floor(Date.now() / 1000) + 1; // 1 second from now

      // First request: 429
      nock('https://api.github.com')
        .get(`/orgs/${TEST_ORG}/repos`)
        .query(true)
        .reply(429, { message: 'rate limit exceeded' }, {
          'X-RateLimit-Remaining': '0',
          'X-RateLimit-Reset': String(resetTime),
        });

      // Retry: success
      nock('https://api.github.com')
        .get(`/orgs/${TEST_ORG}/repos`)
        .query(true)
        .reply(200, [repoFixture('repo-a')]);

      // Branch protection + secret scanning + CODEOWNERS
      nock('https://api.github.com')
        .get(`/repos/${TEST_ORG}/repo-a/branches/main/protection`)
        .reply(200, branchProtectionFixture());
      nock('https://api.github.com')
        .get(`/repos/${TEST_ORG}/repo-a/secret-scanning/alerts`)
        .query(true)
        .reply(200, []);
      nock('https://api.github.com')
        .get(`/repos/${TEST_ORG}/repo-a/contents/CODEOWNERS`)
        .reply(200, { name: 'CODEOWNERS' });

      const evidence = await connector.collect();
      expect(evidence.length).toBeGreaterThan(0);
    }, 10000);
  });

  describe('auth failure', () => {
    it('throws AuthError immediately on 401, no retry', async () => {
      const connector = createConnector();

      nock('https://api.github.com')
        .get(`/orgs/${TEST_ORG}/repos`)
        .query(true)
        .reply(401, { message: 'Bad credentials' });

      await expect(connector.collect()).rejects.toThrow(AuthError);

      // Verify no retry was attempted (nock would throw if an unexpected request came in)
      expect(nock.isDone()).toBe(true);
    });
  });

  describe('healthCheck()', () => {
    it('returns true when rate_limit endpoint returns 200', async () => {
      const connector = createConnector();

      nock('https://api.github.com')
        .get('/rate_limit')
        .reply(200, { rate: { limit: 5000, remaining: 4999 } });

      const result = await connector.healthCheck();
      expect(result).toBe(true);
    });

    it('returns false (not throw) when rate_limit endpoint returns 500', async () => {
      const connector = createConnector();

      nock('https://api.github.com')
        .get('/rate_limit')
        .reply(500, { message: 'Internal Server Error' });

      const result = await connector.healthCheck();
      expect(result).toBe(false);
    });
  });
});
