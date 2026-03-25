# Compliance Engine

AI-powered compliance evidence automation platform. Built to demonstrate the enterprise architecture for automated SOC 2, ISO 27001, PCI-DSS, and ISO 42001 compliance — replacing manual evidence collection with continuous API-driven pipelines, adding an LLM intelligence layer for questionnaire auto-drafting and gap analysis, and enforcing controls directly in CI/CD via OPA policy-as-code.

**Status: Complete** — 4 phases, 57 tests, zero `any`, zero network calls in tests.

## Tech stack
- **Runtime:** Node.js 20 + TypeScript (strict mode, zero `any`)
- **Database:** PostgreSQL 16 + pgvector + Drizzle ORM
- **Queue:** Redis + BullMQ
- **AI:** Anthropic Claude API + Voyage AI embeddings (voyage-3, 1024d)
- **Policy:** OPA Rego + conftest + GitHub Actions
- **Testing:** Jest + nock + supertest (57 tests, zero network calls)
- **Infra:** Docker Compose, pnpm workspaces (9-package monorepo)

## Architecture — 6 systems

| System | Package | Status |
|--------|---------|--------|
| Evidence engine | `packages/evidence-engine` | Complete — 3 connectors (GitHub, Okta, AWS Config), drift detection, BullMQ scheduler |
| AI compliance layer | `packages/ai-layer` | Complete — RAG pipeline (Voyage + pgvector), questionnaire parser, Claude auto-drafter |
| Customer trust center API | `packages/api` | Complete — Express REST API, trust status, questionnaire submit/poll, NDA recording |
| Policy as code | `compliance/policies/` | Complete — OPA Rego (S3 encryption), GitHub Actions compliance gate on every PR |
| Risk dashboard | `packages/api` | Complete — posture score, controls, drift events, gaps by framework, board scorecard |
| Alerting engine | `packages/evidence-engine` | Complete — Slack webhooks (severity colors), Jira tickets (P1/P2/P3), drift event routing |

## Evidence connectors

| Connector | Source | Controls mapped | Schedule | Tests |
|-----------|--------|----------------|----------|-------|
| GitHub | Branch protection, PR enforcement, secret scanning, CODEOWNERS | SOC2-CC8.1, ISO27001-A.14.2.2, PCIDSS-REQ6.3 | Every 4 hours | 7 |
| Okta | MFA enrollment, inactive users, admin roles, session policy | SOC2-CC6.1/CC6.2/CC6.3, ISO27001-A.9.2.3/A.9.2.5/A.9.4.2, PCIDSS-REQ7.1/REQ8.3.2/REQ8.3.9 | Every 6 hours | 9 |
| AWS Config | S3 encryption, security groups, RDS, CloudTrail, VPC flow logs, IAM root, privileged API events | SOC2-CC6.3/CC6.6/CC6.7/CC7.2, ISO27001-A.10.1.1/A.12.4.1/A.12.4.3, PCIDSS-REQ1.2/REQ3.4/REQ7.1/REQ10.1 | Every 30 min | 8 |

## AI compliance layer

- **RAG pipeline:** 500-word sliding window chunking, Voyage AI embeddings, pgvector cosine similarity retrieval
- **Questionnaire parser:** auto-detects text/CSV, extracts structured questions with framework tagging
- **Auto-drafter:** Claude API with static system prompt (prompt injection safe), anti-fabrication rule (`EVIDENCE_MISSING` flags), 4-tier review (auto_approve / sme_review / legal_review / manual)

## REST API endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness check |
| GET | `/api/trust/status` | Live cert status per framework |
| GET | `/api/trust/documents/:type` | NDA-gated document delivery |
| POST | `/api/trust/questionnaire` | Submit questions for auto-drafting |
| GET | `/api/trust/questionnaire/:jobId` | Poll for draft results |
| POST | `/api/trust/nda` | Record NDA signature |
| GET | `/api/dashboard/posture` | Overall compliance score |
| GET | `/api/dashboard/controls` | All controls with current status |
| GET | `/api/dashboard/drift` | Recent drift events (30 days) |
| GET | `/api/dashboard/gaps` | Failed/stale controls by framework |
| GET | `/api/dashboard/scorecard` | Board-level summary with MTTR |

## Phase completion

- **Phase 1** — Monorepo scaffold, shared types, DB schema, GitHub connector, CLI runner
- **Phase 2** — Okta connector, AWS Config connector, 24 tests
- **Phase 3** — RAG pipeline, questionnaire parser, Claude auto-drafter, 39 tests
- **Phase 4** — REST API, alerting engine (Slack + Jira), dashboard, OPA compliance gate, documentation, 57 tests

## Documentation

- [`DECISIONS.md`](DECISIONS.md) — 8 architecture decision records
- [`docs/PCI-SCOPING.md`](docs/PCI-SCOPING.md) — PCI DSS 4.0 scoping (ServiceTitan + Stripe, SAQ A, reqs 6.4.3/11.6.1)
- [`compliance/control-library.md`](compliance/control-library.md) — All 33 controls with evidence sources, owners, cross-framework mappings
- [`RUNBOOK.md`](RUNBOOK.md) — Operations runbook: incident response, connector failures, credential rotation, checklists

## Local setup
```bash
docker-compose up -d
pnpm install
pnpm db:migrate
pnpm db:seed
pnpm build
pnpm test                    # 57 tests, zero network calls
pnpm --filter evidence-engine run connector:run --source=github
pnpm --filter api run start  # Express API on port 3000
```

## Why this exists
Portfolio build demonstrating the compliance automation architecture I would implement as Sr. Manager, Trust & Assurance. The evidence engine pattern is derived from production systems built at Vetria (5-agent AI compliance platform, 178 rules, Claude API). This repo is the enterprise-scale version.
