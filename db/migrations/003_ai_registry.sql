-- 003_ai_registry.sql
-- ISO 42001 AI governance registry tables

CREATE TYPE ai_system_status AS ENUM
  ('draft','under_review','approved','deprecated');
CREATE TYPE ai_risk_tier AS ENUM ('low','medium','high','critical');

CREATE TABLE ai_systems (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name              TEXT NOT NULL,
  description       TEXT,
  repo              TEXT,
  system_type       TEXT NOT NULL,
  risk_tier         ai_risk_tier,
  data_inputs       TEXT[] DEFAULT '{}',
  training_data_src TEXT,
  human_oversight   TEXT,
  output_scope      TEXT,
  regulatory_flags  TEXT[] DEFAULT '{}',
  iso42001_controls JSONB DEFAULT '{}',
  risk_assessment   TEXT,
  pm_owner          TEXT,
  eng_owner         TEXT,
  status            ai_system_status DEFAULT 'draft',
  last_reviewed_at  TIMESTAMPTZ,
  review_due_at     TIMESTAMPTZ,
  created_at        TIMESTAMPTZ DEFAULT now()
);
