import * as https from 'node:https';
import type { Connector, RawEvidence, ControlEvidence } from '@compliance-engine/types';
import { AuthError, RateLimitError, logger } from '@compliance-engine/common';
import { buildControlEvidence } from '../normalizer/normalizer.js';

const MAX_RETRIES = 3;
const BACKOFF_BASE_MS = 1000;
const REQUEST_TIMEOUT_MS = 10000;

// Control ID mappings per evidence category
const MFA_CONTROLS = ['SOC2-CC6.1', 'ISO27001-A.9.4.2', 'PCIDSS-REQ8.3.2'] as const;
const INACTIVE_CONTROLS = ['SOC2-CC6.2', 'ISO27001-A.9.2.5'] as const;
const ADMIN_CONTROLS = ['SOC2-CC6.3', 'ISO27001-A.9.2.3', 'PCIDSS-REQ7.1'] as const;
const SESSION_CONTROLS = ['SOC2-CC6.1', 'PCIDSS-REQ8.3.9'] as const;

const ALL_CONTROL_IDS = [
  ...new Set([...MFA_CONTROLS, ...INACTIVE_CONTROLS, ...ADMIN_CONTROLS, ...SESSION_CONTROLS]),
];

export interface OktaConnectorConfig {
  domain: string;
  token: string;
}

interface HttpResponse {
  status: number;
  headers: Record<string, string | undefined>;
  body: string;
}

interface OktaUser {
  id: string;
  status: string;
  profile: { login: string; firstName: string; lastName: string };
  _embedded?: {
    factors?: Array<{ factorType: string; status: string }>;
    roles?: Array<{ type: string }>;
  };
}

interface MfaStats {
  totalActive: number;
  enrolled: number;
  unenrolled: number;
  enrollmentRate: number;
}

/**
 * Okta evidence connector.
 * Pulls MFA enrollment, inactive accounts, admin roles,
 * MFA policy enforcement, and session management evidence.
 */
export class OktaConnector implements Connector {
  readonly source = 'okta';
  readonly schedule = '0 */6 * * *'; // every 6 hours

  constructor(private readonly config: OktaConnectorConfig) {}

  async collect(): Promise<RawEvidence[]> {
    const [users, mfaPolicies, sessionLogs] = await Promise.all([
      this.getUsers(),
      this.getMfaPolicies(),
      this.getSessionLogs(),
    ]);

    const activeUsers = users.filter((u) => u.status === 'ACTIVE');
    const mfaStats = this.computeMfaStats(activeUsers);
    const inactiveUsers = users.filter(
      (u) => u.status === 'DEPROVISIONED' || u.status === 'SUSPENDED',
    );
    const adminUsers = users.filter((u) => {
      const roles = u._embedded?.roles ?? [];
      return roles.length > 0;
    });

    const now = new Date();

    return [
      {
        source: 'okta',
        collectedAt: now,
        suggestedControlIds: ALL_CONTROL_IDS,
        data: {
          users,
          mfaPolicies,
          sessionLogs,
          mfaStats,
          inactiveUsers,
          adminUsers,
        },
        metadata: {},
      },
    ];
  }

  async normalize(raw: RawEvidence[]): Promise<ControlEvidence[]> {
    const results: ControlEvidence[] = [];

    for (const evidence of raw) {
      const data = evidence.data as Record<string, unknown>;
      const mfaStats = data['mfaStats'] as MfaStats;
      const mfaPolicies = data['mfaPolicies'] as Array<Record<string, unknown>>;

      // MFA confidence scoring
      const policyEnforced = mfaPolicies.some((p) => p['status'] === 'ACTIVE');
      const mfaConfidence = this.calculateMfaConfidence(policyEnforced, mfaStats.enrollmentRate);
      const mfaStatus = this.confidenceToStatus(mfaConfidence);

      // Emit evidence for each control category
      for (const controlId of MFA_CONTROLS) {
        results.push(
          buildControlEvidence({
            controlId,
            framework: this.controlIdToFramework(controlId),
            status: mfaStatus,
            confidence: mfaConfidence,
            raw: evidence,
          }),
        );
      }

      for (const controlId of INACTIVE_CONTROLS) {
        results.push(
          buildControlEvidence({
            controlId,
            framework: this.controlIdToFramework(controlId),
            status: mfaStatus,
            confidence: mfaConfidence,
            raw: evidence,
          }),
        );
      }

      for (const controlId of ADMIN_CONTROLS) {
        results.push(
          buildControlEvidence({
            controlId,
            framework: this.controlIdToFramework(controlId),
            status: mfaStatus,
            confidence: mfaConfidence,
            raw: evidence,
          }),
        );
      }

      for (const controlId of SESSION_CONTROLS) {
        results.push(
          buildControlEvidence({
            controlId,
            framework: this.controlIdToFramework(controlId),
            status: mfaStatus,
            confidence: mfaConfidence,
            raw: evidence,
          }),
        );
      }
    }

    return results;
  }

  async healthCheck(): Promise<boolean> {
    try {
      const response = await this.httpGet('/api/v1/org');
      return response.status === 200;
    } catch {
      return false;
    }
  }

  // -- Private helpers --

  /**
   * MFA confidence scoring:
   *   1.0: policy enforced AND 0% users lack enrollment
   *   0.5: policy enforced but >0% and <=5% users lack MFA
   *   0.0: policy not enforced OR >5% users without MFA
   */
  private calculateMfaConfidence(policyEnforced: boolean, enrollmentRate: number): number {
    if (!policyEnforced) return 0.0;
    const unenrolledPct = Math.round((1 - enrollmentRate) * 100);
    if (unenrolledPct === 0) return 1.0;
    if (unenrolledPct <= 5) return 0.5;
    return 0.0;
  }

  /**
   * Map confidence to status:
   *   >= 0.8 → 'pass'
   *   0.5–0.79 → 'drift'
   *   < 0.5 → 'fail'
   */
  private confidenceToStatus(confidence: number): ControlEvidence['status'] {
    if (confidence >= 0.8) return 'pass';
    if (confidence >= 0.5) return 'drift';
    return 'fail';
  }

  private controlIdToFramework(controlId: string): ControlEvidence['framework'] {
    if (controlId.startsWith('SOC2')) return 'SOC2';
    if (controlId.startsWith('ISO27001')) return 'ISO27001';
    return 'PCIDSS';
  }

  private computeMfaStats(activeUsers: OktaUser[]): MfaStats {
    let enrolled = 0;
    for (const user of activeUsers) {
      const factors = user._embedded?.factors ?? [];
      if (factors.some((f) => f.status === 'ACTIVE')) {
        enrolled++;
      }
    }
    return {
      totalActive: activeUsers.length,
      enrolled,
      unenrolled: activeUsers.length - enrolled,
      enrollmentRate: activeUsers.length > 0 ? enrolled / activeUsers.length : 0,
    };
  }

  private async getUsers(): Promise<OktaUser[]> {
    const allUsers: OktaUser[] = [];
    let path: string | null = '/api/v1/users?limit=200';

    while (path) {
      const response = await this.httpGet(path);
      const data = JSON.parse(response.body) as OktaUser[];
      allUsers.push(...data);
      path = this.parseNextLink(response.headers['link'] ?? null);
    }

    return allUsers;
  }

  private async getMfaPolicies(): Promise<unknown[]> {
    const response = await this.httpGet('/api/v1/policies?type=MFA_ENROLL');
    return JSON.parse(response.body) as unknown[];
  }

  private async getSessionLogs(): Promise<unknown[]> {
    const filter = encodeURIComponent('eventType eq "user.session.start"');
    const response = await this.httpGet(`/api/v1/logs?filter=${filter}&limit=100`);
    return JSON.parse(response.body) as unknown[];
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

  /**
   * Core HTTPS GET with auth, retry on 429/5xx, immediate throw on 401.
   * Uses node:https so nock can intercept in tests.
   */
  private async httpGet(path: string, retryCount = 0): Promise<HttpResponse> {
    const response = await new Promise<HttpResponse>((resolve, reject) => {
      const req = https.request(
        {
          hostname: this.config.domain,
          path,
          method: 'GET',
          headers: {
            Authorization: `SSWS ${this.config.token}`,
            Accept: 'application/json',
            'Content-Type': 'application/json',
            'User-Agent': 'compliance-engine',
          },
          timeout: REQUEST_TIMEOUT_MS,
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

      req.on('timeout', () => {
        req.destroy();
        reject(new Error('Request timed out'));
      });
      req.on('error', reject);
      req.end();
    });

    // 401 — throw immediately, no retry
    if (response.status === 401) {
      throw new AuthError('Okta API authentication failed');
    }

    // 429 — wait Retry-After seconds, then retry
    if (response.status === 429) {
      if (retryCount >= MAX_RETRIES) {
        throw new RateLimitError('Okta rate limit exceeded after max retries', 0);
      }

      const retryAfterSec = Number(response.headers['retry-after'] ?? '1');
      const waitMs = retryAfterSec * 1000;

      logger.warn('Okta rate limit hit, waiting', { waitMs, retryCount });
      await this.sleep(waitMs);
      return this.httpGet(path, retryCount + 1);
    }

    // 5xx — exponential backoff
    if (response.status >= 500) {
      if (retryCount >= MAX_RETRIES) {
        throw new Error(`Okta API server error after ${MAX_RETRIES} retries: ${response.status}`);
      }

      const waitMs = BACKOFF_BASE_MS * Math.pow(2, retryCount);
      logger.warn('Okta server error, retrying', { status: response.status, waitMs, retryCount });
      await this.sleep(waitMs);
      return this.httpGet(path, retryCount + 1);
    }

    return response;
  }

  private sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }
}
