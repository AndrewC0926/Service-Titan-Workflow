import {
  pgTable,
  pgEnum,
  varchar,
  text,
  integer,
  uuid,
  decimal,
  jsonb,
  timestamp,
  index,
} from 'drizzle-orm/pg-core';

// -- Enums matching SQL exactly --

export const frameworkEnum = pgEnum('framework', [
  'SOC2',
  'ISO27001',
  'PCIDSS',
  'ISO42001',
]);

export const controlStatusEnum = pgEnum('control_status', [
  'pass',
  'fail',
  'drift',
  'stale',
  'pending',
]);

export const aiSystemStatusEnum = pgEnum('ai_system_status', [
  'draft',
  'under_review',
  'approved',
  'deprecated',
]);

export const aiRiskTierEnum = pgEnum('ai_risk_tier', [
  'low',
  'medium',
  'high',
  'critical',
]);

// -- Tables --

export const controls = pgTable('controls', {
  id: varchar('id', { length: 30 }).primaryKey(),
  framework: frameworkEnum('framework').notNull(),
  title: text('title').notNull(),
  description: text('description'),
  tier: integer('tier').notNull().default(2),
  testOnceIds: text('test_once_ids').array().default([]),
  createdAt: timestamp('created_at', { withTimezone: true }).defaultNow(),
});

export const controlEvidence = pgTable(
  'control_evidence',
  {
    id: uuid('id').primaryKey().defaultRandom(),
    controlId: varchar('control_id', { length: 30 }).references(() => controls.id),
    source: varchar('source', { length: 50 }).notNull(),
    status: controlStatusEnum('status').notNull(),
    confidence: decimal('confidence', { precision: 4, scale: 3 }).notNull(),
    rawData: jsonb('raw_data').notNull(),
    collectedAt: timestamp('collected_at', { withTimezone: true }).notNull(),
    expiresAt: timestamp('expires_at', { withTimezone: true }).notNull(),
    createdAt: timestamp('created_at', { withTimezone: true }).defaultNow(),
  },
  (table) => [
    index('idx_evidence_control_collected').on(table.controlId, table.collectedAt),
  ],
);

export const controlDriftEvents = pgTable(
  'control_drift_events',
  {
    id: uuid('id').primaryKey().defaultRandom(),
    controlId: varchar('control_id', { length: 30 }).references(() => controls.id),
    prevStatus: controlStatusEnum('prev_status').notNull(),
    newStatus: controlStatusEnum('new_status').notNull(),
    diffPayload: jsonb('diff_payload').notNull(),
    detectedAt: timestamp('detected_at', { withTimezone: true }).defaultNow(),
    resolvedAt: timestamp('resolved_at', { withTimezone: true }),
    jiraTicketId: varchar('jira_ticket_id', { length: 20 }),
  },
  (table) => [
    index('idx_drift_control_detected').on(table.controlId, table.detectedAt),
  ],
);

export const reviewQueue = pgTable('review_queue', {
  id: uuid('id').primaryKey().defaultRandom(),
  jobId: uuid('job_id').notNull(),
  questionId: text('question_id').notNull(),
  questionText: text('question_text').notNull(),
  draftResponse: text('draft_response'),
  confidence: decimal('confidence', { precision: 4, scale: 3 }),
  sources: text('sources').array().default([]),
  flags: text('flags').array().default([]),
  reviewTier: text('review_tier').notNull().default('sme_review'),
  approvedAt: timestamp('approved_at', { withTimezone: true }),
  approvedBy: text('approved_by'),
  finalResponse: text('final_response'),
  createdAt: timestamp('created_at', { withTimezone: true }).defaultNow(),
});

export const documentChunks = pgTable('document_chunks', {
  id: uuid('id').primaryKey().defaultRandom(),
  sourceDoc: text('source_doc').notNull(),
  framework: frameworkEnum('framework'),
  controlIds: text('control_ids').array().default([]),
  chunkIndex: integer('chunk_index').notNull(),
  text: text('text').notNull(),
  // embedding column is vector(1024) — managed via raw SQL migration
  // Drizzle doesn't natively support pgvector; queries use raw SQL for similarity
  ingestedAt: timestamp('ingested_at', { withTimezone: true }).defaultNow(),
});

export const aiSystems = pgTable('ai_systems', {
  id: uuid('id').primaryKey().defaultRandom(),
  name: text('name').notNull(),
  description: text('description'),
  repo: text('repo'),
  systemType: text('system_type').notNull(),
  riskTier: aiRiskTierEnum('risk_tier'),
  dataInputs: text('data_inputs').array().default([]),
  trainingDataSrc: text('training_data_src'),
  humanOversight: text('human_oversight'),
  outputScope: text('output_scope'),
  regulatoryFlags: text('regulatory_flags').array().default([]),
  iso42001Controls: jsonb('iso42001_controls').default({}),
  riskAssessment: text('risk_assessment'),
  pmOwner: text('pm_owner'),
  engOwner: text('eng_owner'),
  status: aiSystemStatusEnum('status').default('draft'),
  lastReviewedAt: timestamp('last_reviewed_at', { withTimezone: true }),
  reviewDueAt: timestamp('review_due_at', { withTimezone: true }),
  createdAt: timestamp('created_at', { withTimezone: true }).defaultNow(),
});
