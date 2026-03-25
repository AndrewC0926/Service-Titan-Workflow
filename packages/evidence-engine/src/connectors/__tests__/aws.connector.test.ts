import { AwsConnector } from '../aws.connector.js';
import { AuthError } from '@compliance-engine/common';
import type { RawEvidence } from '@compliance-engine/types';

// Mock AWS SDK clients
jest.mock('@aws-sdk/client-config-service', () => {
  const mockSend = jest.fn();
  return {
    ConfigServiceClient: jest.fn().mockImplementation(() => ({ send: mockSend })),
    DescribeComplianceByConfigRuleCommand: jest.fn().mockImplementation((input: unknown) => ({
      _type: 'DescribeComplianceByConfigRule',
      input,
    })),
    GetComplianceDetailsByConfigRuleCommand: jest.fn().mockImplementation((input: unknown) => ({
      _type: 'GetComplianceDetailsByConfigRule',
      input,
    })),
    __mockSend: mockSend,
  };
});

jest.mock('@aws-sdk/client-cloudtrail', () => {
  const mockSend = jest.fn();
  return {
    CloudTrailClient: jest.fn().mockImplementation(() => ({ send: mockSend })),
    LookupEventsCommand: jest.fn().mockImplementation((input: unknown) => ({
      _type: 'LookupEvents',
      input,
    })),
    __mockSend: mockSend,
  };
});

// Access the mock send functions
function getConfigMockSend(): jest.Mock {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  return require('@aws-sdk/client-config-service').__mockSend as jest.Mock;
}

function getCloudTrailMockSend(): jest.Mock {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  return require('@aws-sdk/client-cloudtrail').__mockSend as jest.Mock;
}

const TEST_CONFIG = {
  region: 'us-east-1',
  accessKeyId: 'AKIATEST',
  secretAccessKey: 'test-secret',
};

function createConnector(): AwsConnector {
  return new AwsConnector(TEST_CONFIG);
}

// Fixtures
function complianceRuleFixture(
  ruleName: string,
  complianceType: 'COMPLIANT' | 'NON_COMPLIANT',
) {
  return {
    ConfigRuleName: ruleName,
    Compliance: { ComplianceType: complianceType },
  };
}

function privilegedEventFixture(eventName: string) {
  return {
    EventName: eventName,
    EventTime: new Date(),
    Username: 'root',
    Resources: [{ ResourceType: 'AWS::S3::Bucket', ResourceName: 'my-bucket' }],
  };
}

beforeEach(() => {
  getConfigMockSend().mockReset();
  getCloudTrailMockSend().mockReset();
});

describe('AwsConnector', () => {
  describe('collect()', () => {
    it('returns RawEvidence[] with correct shape', async () => {
      const connector = createConnector();

      // Mock DescribeComplianceByConfigRule
      getConfigMockSend().mockResolvedValue({
        ComplianceByConfigRules: [
          complianceRuleFixture('S3_BUCKET_SERVER_SIDE_ENCRYPTION_ENABLED', 'COMPLIANT'),
          complianceRuleFixture('CLOUD_TRAIL_ENABLED', 'COMPLIANT'),
          complianceRuleFixture('RESTRICTED_SSH', 'COMPLIANT'),
          complianceRuleFixture('RDS_STORAGE_ENCRYPTED', 'COMPLIANT'),
          complianceRuleFixture('VPC_FLOW_LOGS_ENABLED', 'COMPLIANT'),
          complianceRuleFixture('IAM_ROOT_ACCESS_KEY_CHECK', 'COMPLIANT'),
        ],
      });

      // Mock LookupEvents (no privileged events)
      getCloudTrailMockSend().mockResolvedValue({
        Events: [],
      });

      const evidence = await connector.collect();

      expect(evidence.length).toBeGreaterThan(0);
      expect(evidence[0]!.source).toBe('aws');
      expect(evidence[0]!.collectedAt).toBeInstanceOf(Date);
      expect(evidence[0]!.data).toHaveProperty('configRules');
      expect(evidence[0]!.data).toHaveProperty('privilegedEvents');
    });
  });

  describe('normalize()', () => {
    it('all COMPLIANT → confidence 1.0, status pass', async () => {
      const connector = createConnector();
      const now = new Date();

      const raw: RawEvidence = {
        source: 'aws',
        collectedAt: now,
        suggestedControlIds: ['SOC2-CC6.7', 'SOC2-CC7.2'],
        data: {
          configRules: [
            complianceRuleFixture('S3_BUCKET_SERVER_SIDE_ENCRYPTION_ENABLED', 'COMPLIANT'),
            complianceRuleFixture('CLOUD_TRAIL_ENABLED', 'COMPLIANT'),
            complianceRuleFixture('RESTRICTED_SSH', 'COMPLIANT'),
            complianceRuleFixture('RDS_STORAGE_ENCRYPTED', 'COMPLIANT'),
            complianceRuleFixture('VPC_FLOW_LOGS_ENABLED', 'COMPLIANT'),
            complianceRuleFixture('IAM_ROOT_ACCESS_KEY_CHECK', 'COMPLIANT'),
          ],
          privilegedEvents: [],
        },
        metadata: {},
      };

      const results = await connector.normalize([raw]);
      const s3Result = results.find((r) => r.controlId === 'SOC2-CC6.7');

      expect(s3Result).toBeDefined();
      expect(s3Result!.confidence).toBe(1.0);
      expect(s3Result!.status).toBe('pass');
    });

    it('critical rule NON_COMPLIANT → confidence 0.0, status fail', async () => {
      const connector = createConnector();
      const now = new Date();

      const raw: RawEvidence = {
        source: 'aws',
        collectedAt: now,
        suggestedControlIds: ['SOC2-CC6.7'],
        data: {
          configRules: [
            complianceRuleFixture('S3_BUCKET_SERVER_SIDE_ENCRYPTION_ENABLED', 'NON_COMPLIANT'),
            complianceRuleFixture('CLOUD_TRAIL_ENABLED', 'COMPLIANT'),
            complianceRuleFixture('RESTRICTED_SSH', 'COMPLIANT'),
            complianceRuleFixture('RDS_STORAGE_ENCRYPTED', 'COMPLIANT'),
            complianceRuleFixture('VPC_FLOW_LOGS_ENABLED', 'COMPLIANT'),
            complianceRuleFixture('IAM_ROOT_ACCESS_KEY_CHECK', 'COMPLIANT'),
          ],
          privilegedEvents: [],
        },
        metadata: {},
      };

      const results = await connector.normalize([raw]);
      const s3Result = results.find((r) => r.controlId === 'SOC2-CC6.7');

      expect(s3Result).toBeDefined();
      expect(s3Result!.confidence).toBe(0.0);
      expect(s3Result!.status).toBe('fail');
    });

    it('non-critical NON_COMPLIANT → confidence 0.7, status drift', async () => {
      const connector = createConnector();
      const now = new Date();

      const raw: RawEvidence = {
        source: 'aws',
        collectedAt: now,
        suggestedControlIds: ['SOC2-CC6.7'],
        data: {
          configRules: [
            complianceRuleFixture('S3_BUCKET_SERVER_SIDE_ENCRYPTION_ENABLED', 'COMPLIANT'),
            complianceRuleFixture('CLOUD_TRAIL_ENABLED', 'COMPLIANT'),
            complianceRuleFixture('RESTRICTED_SSH', 'COMPLIANT'),
            complianceRuleFixture('RDS_STORAGE_ENCRYPTED', 'NON_COMPLIANT'),
            complianceRuleFixture('VPC_FLOW_LOGS_ENABLED', 'COMPLIANT'),
            complianceRuleFixture('IAM_ROOT_ACCESS_KEY_CHECK', 'COMPLIANT'),
          ],
          privilegedEvents: [],
        },
        metadata: {},
      };

      const results = await connector.normalize([raw]);
      // RDS is non-critical, so confidence should be 0.7
      const result = results.find((r) => r.controlId === 'SOC2-CC6.7');

      expect(result).toBeDefined();
      expect(result!.confidence).toBe(0.7);
      expect(result!.status).toBe('drift');
    });
  });

  describe('CloudTrail privileged events', () => {
    it('includes privileged events in raw data', async () => {
      const connector = createConnector();

      getConfigMockSend().mockResolvedValue({
        ComplianceByConfigRules: [
          complianceRuleFixture('S3_BUCKET_SERVER_SIDE_ENCRYPTION_ENABLED', 'COMPLIANT'),
          complianceRuleFixture('CLOUD_TRAIL_ENABLED', 'COMPLIANT'),
          complianceRuleFixture('RESTRICTED_SSH', 'COMPLIANT'),
        ],
      });

      getCloudTrailMockSend().mockResolvedValue({
        Events: [
          privilegedEventFixture('DeleteTrail'),
          privilegedEventFixture('StopLogging'),
        ],
      });

      const evidence = await connector.collect();
      const events = (evidence[0]!.data as Record<string, unknown>)[
        'privilegedEvents'
      ] as unknown[];
      expect(events).toHaveLength(2);
    });
  });

  describe('SDK error (AccessDenied)', () => {
    it('throws AuthError on AccessDeniedException', async () => {
      const connector = createConnector();

      const accessDenied = new Error('Access Denied');
      accessDenied.name = 'AccessDeniedException';
      getConfigMockSend().mockRejectedValue(accessDenied);
      getCloudTrailMockSend().mockRejectedValue(accessDenied);

      await expect(connector.collect()).rejects.toThrow(AuthError);
    });
  });

  describe('healthCheck()', () => {
    it('returns true when SDK responds', async () => {
      const connector = createConnector();

      getConfigMockSend().mockResolvedValue({
        ComplianceByConfigRules: [],
      });

      const result = await connector.healthCheck();
      expect(result).toBe(true);
    });

    it('returns false when SDK throws', async () => {
      const connector = createConnector();

      getConfigMockSend().mockRejectedValue(new Error('Service unavailable'));

      const result = await connector.healthCheck();
      expect(result).toBe(false);
    });
  });
});
