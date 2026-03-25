import {
  ConfigServiceClient,
  DescribeComplianceByConfigRuleCommand,
} from '@aws-sdk/client-config-service';
import {
  CloudTrailClient,
  LookupEventsCommand,
} from '@aws-sdk/client-cloudtrail';
import type { Connector, RawEvidence, ControlEvidence } from '@compliance-engine/types';
import { AuthError, logger } from '@compliance-engine/common';
import { buildControlEvidence } from '../normalizer/normalizer.js';

// Critical rules — any NON_COMPLIANT = confidence 0.0
const CRITICAL_RULES = new Set([
  'S3_BUCKET_SERVER_SIDE_ENCRYPTION_ENABLED',
  'CLOUD_TRAIL_ENABLED',
  'RESTRICTED_SSH',
]);

// Privileged API events to watch for
const PRIVILEGED_EVENTS = ['DeleteTrail', 'StopLogging', 'PutBucketPolicy'];

// Control ID mappings per Config rule / evidence type
const RULE_CONTROL_MAP: Record<string, readonly string[]> = {
  S3_BUCKET_SERVER_SIDE_ENCRYPTION_ENABLED: ['SOC2-CC6.7', 'ISO27001-A.10.1.1', 'PCIDSS-REQ3.4'],
  RESTRICTED_SSH: ['SOC2-CC6.6', 'PCIDSS-REQ1.2'],
  RDS_STORAGE_ENCRYPTED: ['SOC2-CC6.7', 'ISO27001-A.10.1.1', 'PCIDSS-REQ3.4'],
  CLOUD_TRAIL_ENABLED: ['SOC2-CC7.2', 'ISO27001-A.12.4.1', 'PCIDSS-REQ10.1'],
  VPC_FLOW_LOGS_ENABLED: ['SOC2-CC6.6', 'PCIDSS-REQ1.2'],
  IAM_ROOT_ACCESS_KEY_CHECK: ['SOC2-CC6.3', 'PCIDSS-REQ7.1'],
};

const PRIVILEGED_EVENT_CONTROLS = ['SOC2-CC7.2', 'ISO27001-A.12.4.3'] as const;

// Deduplicated set of all control IDs
const ALL_CONTROL_IDS = [
  ...new Set([
    ...Object.values(RULE_CONTROL_MAP).flat(),
    ...PRIVILEGED_EVENT_CONTROLS,
  ]),
];

export interface AwsConnectorConfig {
  region: string;
  accessKeyId: string;
  secretAccessKey: string;
}

interface ConfigRuleResult {
  ConfigRuleName: string;
  Compliance: { ComplianceType: string };
}

/**
 * AWS Config + CloudTrail evidence connector.
 * Pulls Config rule compliance status and CloudTrail privileged API events.
 */
export class AwsConnector implements Connector {
  readonly source = 'aws';
  readonly schedule = '*/30 * * * *'; // every 30 minutes

  private readonly configClient: ConfigServiceClient;
  private readonly cloudTrailClient: CloudTrailClient;

  constructor(private readonly config: AwsConnectorConfig) {
    const credentials = {
      accessKeyId: config.accessKeyId,
      secretAccessKey: config.secretAccessKey,
    };

    this.configClient = new ConfigServiceClient({
      region: config.region,
      credentials,
    });

    this.cloudTrailClient = new CloudTrailClient({
      region: config.region,
      credentials,
    });
  }

  async collect(): Promise<RawEvidence[]> {
    const [configRules, privilegedEvents] = await Promise.all([
      this.getConfigCompliance(),
      this.getPrivilegedEvents(),
    ]);

    const now = new Date();

    return [
      {
        source: 'aws',
        collectedAt: now,
        suggestedControlIds: ALL_CONTROL_IDS,
        data: {
          configRules,
          privilegedEvents,
        },
        metadata: {},
      },
    ];
  }

  async normalize(raw: RawEvidence[]): Promise<ControlEvidence[]> {
    const results: ControlEvidence[] = [];

    for (const evidence of raw) {
      const data = evidence.data as Record<string, unknown>;
      const configRules = data['configRules'] as ConfigRuleResult[];
      const privilegedEvents = data['privilegedEvents'] as unknown[];

      const confidence = this.calculateConfidence(configRules, privilegedEvents);
      const status = this.confidenceToStatus(confidence);

      // Emit evidence for all mapped controls
      for (const controlId of ALL_CONTROL_IDS) {
        results.push(
          buildControlEvidence({
            controlId,
            framework: this.controlIdToFramework(controlId),
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
      await this.configClient.send(
        new DescribeComplianceByConfigRuleCommand({}),
      );
      return true;
    } catch {
      return false;
    }
  }

  // -- Private helpers --

  /**
   * Confidence scoring:
   *   1.0: all Config rules COMPLIANT, zero privileged events
   *   0.7: 1-2 non-critical Config rules NON_COMPLIANT
   *   0.0: any critical rule NON_COMPLIANT
   */
  private calculateConfidence(
    configRules: ConfigRuleResult[],
    privilegedEvents: unknown[],
  ): number {
    // Check for critical rule failures first
    for (const rule of configRules) {
      if (
        CRITICAL_RULES.has(rule.ConfigRuleName) &&
        rule.Compliance.ComplianceType === 'NON_COMPLIANT'
      ) {
        return 0.0;
      }
    }

    // Count non-critical failures
    const nonCriticalFailures = configRules.filter(
      (r) =>
        !CRITICAL_RULES.has(r.ConfigRuleName) &&
        r.Compliance.ComplianceType === 'NON_COMPLIANT',
    ).length;

    // Any privileged events = reduce confidence
    if (privilegedEvents.length > 0) {
      return 0.0;
    }

    if (nonCriticalFailures === 0) return 1.0;
    if (nonCriticalFailures <= 2) return 0.7;
    return 0.0;
  }

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

  private async getConfigCompliance(): Promise<ConfigRuleResult[]> {
    try {
      const response = await this.configClient.send(
        new DescribeComplianceByConfigRuleCommand({}),
      );

      return (response.ComplianceByConfigRules ?? []).map((rule) => ({
        ConfigRuleName: rule.ConfigRuleName ?? '',
        Compliance: {
          ComplianceType: rule.Compliance?.ComplianceType ?? 'INSUFFICIENT_DATA',
        },
      }));
    } catch (err: unknown) {
      if (err instanceof Error && err.name === 'AccessDeniedException') {
        throw new AuthError('AWS Config access denied');
      }
      throw err;
    }
  }

  private async getPrivilegedEvents(): Promise<unknown[]> {
    try {
      const lookupAttributes = PRIVILEGED_EVENTS.map((eventName) => ({
        AttributeKey: 'EventName' as const,
        AttributeValue: eventName,
      }));

      const now = new Date();
      const oneDayAgo = new Date(now.getTime() - 24 * 60 * 60 * 1000);

      const response = await this.cloudTrailClient.send(
        new LookupEventsCommand({
          LookupAttributes: lookupAttributes,
          StartTime: oneDayAgo,
          EndTime: now,
        }),
      );

      return response.Events ?? [];
    } catch (err: unknown) {
      if (err instanceof Error && err.name === 'AccessDeniedException') {
        throw new AuthError('AWS CloudTrail access denied');
      }
      logger.warn('Failed to fetch CloudTrail events', {
        error: err instanceof Error ? err.message : String(err),
      });
      return [];
    }
  }
}
