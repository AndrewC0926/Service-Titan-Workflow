import * as https from 'node:https';
import type { Connector, RawEvidence, ControlEvidence } from '@compliance-engine/types';
import { AuthError, RateLimitError, logger } from '@compliance-engine/common';
import { buildControlEvidence } from '../normalizer/normalizer.js';

const GITHUB_API_HOST = 'api.github.com';
const MAX_RETRIES = 3;
const BACKOFF_BASE_MS = 1000;

const MAPPED_CONTROLS = ['SOC2-CC8.1', 'ISO27001-A.14.2.2', 'PCIDSS-REQ6.3'] as const;

export interface GitHubConnectorConfig {
  token: string;
  org: string;
  maxRepos: number;
}

interface HttpResponse {
  status: number;
  headers: Record<string, string | undefined>;
  body: string;
}

interface RepoData {
  name: string;
  defaultBranch: string;
  branchProtection: {
    enabled: boolean;
    enforceAdmins: boolean;
    requiredReviews: boolean;
    requiredApprovingCount: number;
  };
  secretAlerts: unknown[];
  hasCodeowners: boolean;
}

/**
 * GitHub evidence connector — reference implementation.
 * Pulls branch protection, PR approval enforcement, secret scanning alerts,
 * and CODEOWNERS presence from all repos in the configured org.
 */
export class GitHubConnector implements Connector {
  readonly source = 'github';
  readonly schedule = '0 */4 * * *'; // every 4 hours

  constructor(private readonly config: GitHubConnectorConfig) {}

  async collect(): Promise<RawEvidence[]> {
    const repos = await this.listRepos();
    const repoDataList: RepoData[] = [];

    for (const repo of repos) {
      const defaultBranch = (repo as Record<string, unknown>)['default_branch'] as string;
      const name = (repo as Record<string, unknown>)['name'] as string;

      const [branchProtection, secretAlerts, hasCodeowners] = await Promise.all([
        this.getBranchProtection(name, defaultBranch),
        this.getSecretAlerts(name),
        this.checkCodeowners(name),
      ]);

      repoDataList.push({
        name,
        defaultBranch,
        branchProtection,
        secretAlerts,
        hasCodeowners,
      });
    }

    const now = new Date();

    return [
      {
        source: 'github',
        collectedAt: now,
        suggestedControlIds: [...MAPPED_CONTROLS],
        data: {
          repos: repoDataList,
          totalRepos: repoDataList.length,
          reposChecked: repoDataList.length,
        },
        metadata: {},
      },
    ];
  }

  async normalize(raw: RawEvidence[]): Promise<ControlEvidence[]> {
    const results: ControlEvidence[] = [];

    for (const evidence of raw) {
      const repos = (evidence.data as Record<string, unknown>)['repos'] as RepoData[];
      const confidence = this.calculateConfidence(repos);
      const status = confidence >= 0.8 ? 'pass' : 'fail';

      for (const controlId of MAPPED_CONTROLS) {
        const framework = controlId.startsWith('SOC2')
          ? 'SOC2'
          : controlId.startsWith('ISO27001')
            ? 'ISO27001'
            : 'PCIDSS';

        results.push(
          buildControlEvidence({
            controlId,
            framework,
            status,
            confidence,
            raw: evidence,
          }),
        );
      }
    }

    return results;
  }

  async healthCheck(): Promise<boolean> {
    try {
      const response = await this.httpGet('/rate_limit');
      return response.status === 200;
    } catch {
      return false;
    }
  }

  // -- Private helpers --

  /**
   * Confidence scoring:
   *   1.0 = branch protection on all default branches + PR approval enforced + CODEOWNERS
   *   0.5 = partial enforcement (some checks missing)
   *   0.0 = no branch protection or no PR approval
   */
  private calculateConfidence(repos: RepoData[]): number {
    if (repos.length === 0) return 0;

    let fullyProtected = 0;
    let partiallyProtected = 0;

    for (const repo of repos) {
      if (!repo.branchProtection.enabled) {
        // No protection = contributes 0
        continue;
      }

      const allChecks =
        repo.branchProtection.requiredReviews &&
        repo.branchProtection.enforceAdmins &&
        repo.hasCodeowners;

      if (allChecks) {
        fullyProtected++;
      } else {
        partiallyProtected++;
      }
    }

    // 1.0: all repos fully protected
    if (fullyProtected === repos.length) return 1.0;
    // 0.5: at least some protection exists
    if (fullyProtected + partiallyProtected > 0) return 0.5;
    // 0.0: no protection at all
    return 0.0;
  }

  private async listRepos(): Promise<unknown[]> {
    const allRepos: unknown[] = [];
    let path: string | null = `/orgs/${this.config.org}/repos?per_page=100`;

    while (path) {
      const response = await this.httpGet(path);

      if (response.status === 401) {
        throw new AuthError('GitHub API authentication failed');
      }

      const data = JSON.parse(response.body) as unknown[];
      allRepos.push(...data);

      if (allRepos.length >= this.config.maxRepos) {
        logger.warn('Repo count exceeds MAX_REPOS cap', {
          org: this.config.org,
          maxRepos: this.config.maxRepos,
          found: allRepos.length,
        });
        break;
      }

      path = this.parseNextLink(response.headers['link'] ?? null);
    }

    return allRepos;
  }

  private parseNextLink(linkHeader: string | null): string | null {
    if (!linkHeader) return null;

    const match = linkHeader.match(/<([^>]+)>;\s*rel="next"/);
    if (!match?.[1]) return null;

    const fullUrl = match[1];
    try {
      const parsed = new URL(fullUrl);
      return parsed.pathname + parsed.search;
    } catch {
      return null;
    }
  }

  private async getBranchProtection(
    repo: string,
    branch: string,
  ): Promise<RepoData['branchProtection']> {
    try {
      const response = await this.httpGet(
        `/repos/${this.config.org}/${repo}/branches/${branch}/protection`,
      );

      if (response.status !== 200) {
        return { enabled: false, enforceAdmins: false, requiredReviews: false, requiredApprovingCount: 0 };
      }

      const data = JSON.parse(response.body) as Record<string, unknown>;
      const enforceAdmins = data['enforce_admins'] as Record<string, unknown> | undefined;
      const reviews = data['required_pull_request_reviews'] as Record<string, unknown> | null;

      return {
        enabled: true,
        enforceAdmins: Boolean(enforceAdmins?.['enabled']),
        requiredReviews: reviews !== null && reviews !== undefined,
        requiredApprovingCount: reviews
          ? Number(reviews['required_approving_review_count'] ?? 0)
          : 0,
      };
    } catch {
      return { enabled: false, enforceAdmins: false, requiredReviews: false, requiredApprovingCount: 0 };
    }
  }

  private async getSecretAlerts(repo: string): Promise<unknown[]> {
    try {
      const response = await this.httpGet(
        `/repos/${this.config.org}/${repo}/secret-scanning/alerts?state=open`,
      );
      if (response.status !== 200) return [];
      return JSON.parse(response.body) as unknown[];
    } catch {
      return [];
    }
  }

  private async checkCodeowners(repo: string): Promise<boolean> {
    try {
      const response = await this.httpGet(
        `/repos/${this.config.org}/${repo}/contents/CODEOWNERS`,
      );
      return response.status === 200;
    } catch {
      return false;
    }
  }

  /**
   * Core HTTPS GET with auth, retry on 429/5xx, immediate throw on 401.
   * Uses node:https so nock can intercept in tests.
   */
  private async httpGet(path: string, retryCount = 0): Promise<HttpResponse> {
    const response = await new Promise<HttpResponse>((resolve, reject) => {
      const req = https.request(
        {
          hostname: GITHUB_API_HOST,
          path,
          method: 'GET',
          headers: {
            Authorization: `Bearer ${this.config.token}`,
            Accept: 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'compliance-engine',
          },
        },
        (res) => {
          const chunks: Buffer[] = [];
          res.on('data', (chunk: Buffer) => chunks.push(chunk));
          res.on('end', () => {
            const headers: Record<string, string | undefined> = {};
            for (const [key, value] of Object.entries(res.headers)) {
              headers[key] = Array.isArray(value) ? value[0] : value;
            }
            resolve({
              status: res.statusCode ?? 0,
              headers,
              body: Buffer.concat(chunks).toString('utf-8'),
            });
          });
        },
      );

      req.on('error', reject);
      req.end();
    });

    // 401 — throw immediately, no retry
    if (response.status === 401) {
      throw new AuthError('GitHub API authentication failed');
    }

    // 429 — wait until X-RateLimit-Reset, then retry
    if (response.status === 429) {
      if (retryCount >= MAX_RETRIES) {
        throw new RateLimitError('GitHub rate limit exceeded after max retries', 0);
      }

      const resetEpoch = Number(response.headers['x-ratelimit-reset'] ?? '0');
      const waitMs = Math.max(resetEpoch * 1000 - Date.now(), 100);

      logger.warn('GitHub rate limit hit, waiting', { waitMs, retryCount });
      await this.sleep(waitMs);
      return this.httpGet(path, retryCount + 1);
    }

    // 5xx — exponential backoff
    if (response.status >= 500) {
      if (retryCount >= MAX_RETRIES) {
        throw new Error(`GitHub API server error after ${MAX_RETRIES} retries: ${response.status}`);
      }

      const waitMs = BACKOFF_BASE_MS * Math.pow(2, retryCount);
      logger.warn('GitHub server error, retrying', { status: response.status, waitMs, retryCount });
      await this.sleep(waitMs);
      return this.httpGet(path, retryCount + 1);
    }

    return response;
  }

  private sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }
}
