-- 001_initial_schema.sql
-- Core tables: controls, evidence, drift events, review queue

CREATE TYPE framework AS ENUM ('SOC2', 'ISO27001', 'PCIDSS', 'ISO42001');
CREATE TYPE control_status AS ENUM ('pass','fail','drift','stale','pending');

-- Master control library
CREATE TABLE controls (
  id           VARCHAR(30) PRIMARY KEY,  -- e.g. "SOC2-CC6.1"
  framework    framework NOT NULL,
  title        TEXT NOT NULL,
  description  TEXT,
  tier         INTEGER NOT NULL DEFAULT 2, -- 1=critical, 2=high, 3=medium
  test_once_ids TEXT[] DEFAULT '{}',
  created_at   TIMESTAMPTZ DEFAULT now()
);

-- Live evidence store
CREATE TABLE control_evidence (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  control_id   VARCHAR(30) REFERENCES controls(id),
  source       VARCHAR(50) NOT NULL,
  status       control_status NOT NULL,
  confidence   DECIMAL(4,3) NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  raw_data     JSONB NOT NULL,
  collected_at TIMESTAMPTZ NOT NULL,
  expires_at   TIMESTAMPTZ NOT NULL,
  created_at   TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_evidence_control_collected
  ON control_evidence(control_id, collected_at DESC);

-- Drift event log
CREATE TABLE control_drift_events (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  control_id    VARCHAR(30) REFERENCES controls(id),
  prev_status   control_status NOT NULL,
  new_status    control_status NOT NULL,
  diff_payload  JSONB NOT NULL,
  detected_at   TIMESTAMPTZ DEFAULT now(),
  resolved_at   TIMESTAMPTZ,
  jira_ticket_id VARCHAR(20)
);
CREATE INDEX idx_drift_control_detected
  ON control_drift_events(control_id, detected_at DESC);

-- Review queue for AI-drafted questionnaire responses
CREATE TABLE review_queue (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id          UUID NOT NULL,
  question_id     TEXT NOT NULL,
  question_text   TEXT NOT NULL,
  draft_response  TEXT,
  confidence      DECIMAL(4,3),
  sources         TEXT[] DEFAULT '{}',
  flags           TEXT[] DEFAULT '{}',
  review_tier     TEXT NOT NULL DEFAULT 'sme_review',
  approved_at     TIMESTAMPTZ,
  approved_by     TEXT,
  final_response  TEXT,
  created_at      TIMESTAMPTZ DEFAULT now()
);
