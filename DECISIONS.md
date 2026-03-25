# Architecture Decision Records

## ADR-001: neverthrow over try/catch for typed error contracts

**Status:** Accepted
**Date:** 2026-03-24
**Context:** TypeScript's `try/catch` loses type information — catch blocks receive `unknown` and callers have no compile-time visibility into which errors a function can produce.
**Decision:** Use `neverthrow` (`Result<T, E>`) in `packages/common` for operations where callers need to handle specific error variants. Functions that fail fatally (boot validation, unrecoverable state) still throw typed `AppError` subclasses.
**Consequences:** Callers must explicitly handle both success and error paths. Forces exhaustive error handling at compile time. Adds one dependency but eliminates an entire class of unhandled-rejection bugs.

## ADR-002: Voyage-3 over OpenAI embeddings for RAG

**Status:** Accepted
**Date:** 2026-03-25
**Context:** RAG pipeline needs embeddings for compliance policy documents. Options: OpenAI `text-embedding-3-large` (3072d), Voyage `voyage-3` (1024d), Cohere `embed-v3`.
**Decision:** Voyage-3 at 1024 dimensions. Voyage models are optimized for retrieval tasks over domain-specific corpora (legal, compliance, regulatory text). Lower dimensionality means faster similarity search and smaller pgvector index without sacrificing retrieval quality on compliance documents.
**Consequences:** Vendor dependency on Voyage AI. 1024d embeddings stored in pgvector `vector(1024)` column. If Voyage becomes unavailable, switching to OpenAI embeddings would require re-ingesting all documents and updating the vector column dimension.

## ADR-003: pgvector over Pinecone for vector storage

**Status:** Accepted
**Date:** 2026-03-24
**Context:** RAG retrieval requires vector similarity search. Options: Pinecone (managed), Weaviate (self-hosted), pgvector (Postgres extension).
**Decision:** pgvector in the existing Postgres 16 instance. Compliance data already lives in Postgres (controls, evidence, drift events). Adding a separate vector database introduces operational complexity, another service to secure, and data synchronization concerns — all for a corpus of ~10K document chunks.
**Consequences:** Single database for all data. No additional infrastructure. IVFFlat index with 100 lists is sufficient for our scale. If we exceed ~1M chunks, we'd need to evaluate HNSW index or a dedicated vector database.

## ADR-004: Static system prompts for prompt injection safety

**Status:** Accepted
**Date:** 2026-03-25
**Context:** The questionnaire auto-drafter processes user-provided question text and RAG-retrieved policy documents. Both are attack surfaces for prompt injection — adversarial text in a questionnaire or policy document could override system instructions.
**Decision:** System prompts are static string constants in `drafter.prompt.ts`. All external content (questions, RAG context) goes in the user turn only. No string interpolation of external data into system prompts under any circumstances.
**Consequences:** System behavior cannot be customized per-request. This is intentional — a compliance AI that can be steered by input data is a liability. The anti-fabrication rule ("EVIDENCE_MISSING: [topic]") is enforced at the system prompt level where it cannot be overridden.

## ADR-005: Conservative confidence thresholds (0.85/0.75) and review tiers

**Status:** Accepted
**Date:** 2026-03-25
**Context:** The drafter produces AI-generated responses to compliance questionnaires. These responses go to customers and auditors. A fabricated or unsupported claim in a compliance response is a material misrepresentation.
**Decision:** Four-tier review system with conservative thresholds:
- `auto_approve` (>= 0.85, no flags): strong RAG evidence, no gaps
- `sme_review` (>= 0.75, no flags): moderate evidence, SME validates
- `legal_review` (< 0.75 or any flags): insufficient evidence or gaps detected
- `manual` (confidence = 0): no RAG results at all, human writes from scratch

**Consequences:** Most responses will require human review. This is correct — the system drafts, humans approve. Over time, as the RAG corpus grows, more responses will reach `auto_approve`. The `EVIDENCE_MISSING` flag mechanism ensures gaps are surfaced rather than papered over.

## ADR-006: Test Once Comply Many — normalizer owns cross-framework mapping

**Status:** Accepted
**Date:** 2026-03-24
**Context:** Many compliance controls overlap across frameworks (e.g., "enforce MFA" appears in SOC2-CC6.3, ISO27001-A.9.4.2, and PCIDSS-REQ8.3.9). Collecting evidence separately for each would be redundant.
**Decision:** Each control has a `test_once_ids` array mapping equivalent controls across frameworks. The normalizer in `buildControlEvidence()` produces one evidence record per control, and the `test_once_ids` field allows the GRC platform to satisfy multiple framework requirements from a single evidence artifact.
**Consequences:** Evidence collection runs once per source system. Cross-framework mapping is maintained in the seed data, not in connector logic. Adding a new framework requires updating the seed mappings, not modifying connectors.

## ADR-007: Connector-per-source pattern over a generic connector

**Status:** Accepted
**Date:** 2026-03-24
**Context:** Each source system (GitHub, Okta, AWS) has different authentication, pagination, rate limiting, and data models. A generic connector with config-driven behavior would require extensive abstraction.
**Decision:** One concrete class per source implementing the `Connector` interface. Each handles its own auth, pagination, retry, and confidence scoring. The shared interface (`collect`, `normalize`, `healthCheck`) provides uniformity for the scheduler and registry.
**Consequences:** Some code duplication in HTTP/retry logic across connectors (mitigated by the shared `httpGet` pattern). Adding a new source requires writing a new class, not extending configuration. Each connector is independently testable with source-specific fixtures.

## ADR-008: nock over real HTTP in tests (zero network calls policy)

**Status:** Accepted
**Date:** 2026-03-24
**Context:** Tests must be runnable with no network access, no real credentials, and no external service dependencies. Real HTTP calls make tests flaky, slow, and credential-dependent.
**Decision:** All HTTP-based connectors use `node:https` (not native `fetch`) so that `nock` can intercept at the `http.ClientRequest` level. AWS SDK calls are mocked with `jest.mock`. Anthropic SDK calls are mocked with `jest.mock`. Zero real network calls in the entire test suite.
**Consequences:** nock v13 limitation: cannot intercept native `fetch()`, so connectors must use `node:https`. This is a minor ergonomic cost for complete test isolation. All 39+ tests run in <6 seconds with no network dependency.
