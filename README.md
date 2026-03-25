# Compliance Engine

AI-powered compliance evidence automation platform. Built to demonstrate the enterprise architecture for automated SOC 2, ISO 27001, PCI-DSS, and ISO 42001 compliance — replacing manual evidence collection with continuous API-driven pipelines, adding an LLM intelligence layer for questionnaire auto-drafting and gap analysis, and enforcing controls directly in CI/CD via OPA policy-as-code.

## Tech stack
- **Runtime:** Node.js 20 + TypeScript (strict mode, zero `any`)
- **Database:** PostgreSQL 16 + pgvector + Drizzle ORM
- **Queue:** Redis + BullMQ
- **AI:** Anthropic Claude API + Voyage embeddings
- **Testing:** Jest + nock (zero network calls in tests)
- **Infra:** Docker Compose, pnpm workspaces (monorepo)

## Architecture — 6 systems
1. **Evidence engine** — API connectors pull compliance evidence from source systems (Okta, AWS Config, GitHub, Jira, Stripe, Crowdstrike) on schedule, normalize to unified control library, detect drift in real time
2. **AI compliance layer** — RAG pipeline over policy docs powers questionnaire auto-drafter, nightly gap analysis, drift prediction
3. **Customer trust center API** — headless REST API serving live cert status, NDA-gated docs, Salesforce webhook on prospect engagement
4. **Policy as code** — OPA Rego generator converts control library into CI/CD enforcement rules; compliance gate on every PR
5. **Risk dashboard** — Slack alerting, auto Jira tickets, monthly board scorecard
6. **ISO 42001 AI registry** — auto-discovers AI workloads, intake forms via Jira webhook, Claude-powered risk assessments

## Phase 1 — Complete ✓
- [x] pnpm monorepo, 9 packages, TypeScript strict, zero `any`
- [x] Docker Compose: Postgres 16 + pgvector + Redis
- [x] Shared types: `Framework`, `ControlStatus`, `RawEvidence`, `ControlEvidence`, `ControlDriftEvent`, `Connector` interface
- [x] Common utils: typed logger, zod env validation, neverthrow `Result<T,E>`
- [x] DB package: Drizzle schema + migration runner + seed runner
- [x] 3 SQL migrations + 31 control seed records (SOC2 / ISO27001 / PCIDSS / ISO42001 with cross-framework mappings)
- [x] GitHub connector: pagination, rate-limit retry, auth error handling, confidence scoring
- [x] 7 tests: nock, zero network calls, all passing
- [x] CLI runner: `connector:run --source=github` writes to DB

## Phase 2 — In progress
- [ ] Okta connector (MFA enrollment, access reviews, privileged access)
- [ ] AWS Config connector (infrastructure posture, PCI network segmentation)
- [ ] AI questionnaire auto-drafter (Claude API + RAG + pgvector)

## Local setup
```bash
docker-compose up -d
pnpm install
pnpm db:migrate
pnpm db:seed
pnpm test
pnpm --filter evidence-engine run connector:run --source=github
```

## Why this exists
Portfolio build demonstrating the compliance automation architecture I would implement as Sr. Manager, Trust & Assurance. The evidence engine pattern is derived from production systems built at Vetria (5-agent AI compliance platform, 178 rules, Claude API). This repo is the enterprise-scale version.
