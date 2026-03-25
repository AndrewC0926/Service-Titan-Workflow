# Architecture Decision Records

Every non-trivial decision in this codebase is documented here with the reasoning behind it. The goal is not just to show what was built, but how the architect thinks about tradeoffs.

---

## ADR-001: neverthrow over try/catch

**Why:** Typed error handling forces callers to handle both success and failure paths. try/catch is invisible at the type level — you can't tell from a function signature whether it throws. Compliance systems need explicit error contracts because an unhandled rejection in an evidence pipeline means silently missing a control assessment. `Result<T, E>` makes error handling a compile-time concern, not a runtime surprise.

**Tradeoff:** Adds a dependency and a slight learning curve for developers unfamiliar with Result types. Worth it — the alternative is a class of bugs that only surfaces in production under error conditions.

---

## ADR-002: Voyage-3 over OpenAI embeddings for RAG

**Why:** Voyage-3 is optimized for retrieval tasks in technical domains. Compliance documents contain dense regulatory language — domain-specific embeddings outperform general-purpose ones on cosine similarity for policy text. At 1024 dimensions (vs OpenAI's 3072), we get faster similarity search and a smaller pgvector index without sacrificing retrieval quality on the document types we actually care about.

**Tradeoff:** Vendor dependency on Voyage AI. If they go down, we re-ingest with OpenAI embeddings and update the vector column dimension. Acceptable because embeddings are a batch operation, not a real-time dependency.

---

## ADR-003: pgvector over Pinecone

**Why:** We're already running Postgres. Adding a separate vector database is operational complexity with no benefit at this scale. pgvector with IVFFlat indexing handles millions of chunks — ServiceTitan's policy library won't exceed tens of thousands. One database means one backup strategy, one connection pool, one set of credentials to rotate, one service to monitor. In compliance, every additional service is another attack surface to document and audit.

**Tradeoff:** If we exceed ~1M chunks, IVFFlat performance degrades and we'd evaluate HNSW or a dedicated vector DB. That's a problem we'd love to have.

---

## ADR-004: Static system prompts — prompt injection safety

**Why:** User-provided questionnaire content goes in the user turn only. A questionnaire that contains "ignore previous instructions" in a question must not be able to override the compliance-grounded system prompt. This is not theoretical — adversarial content injection into AI systems is a documented attack vector, and a compliance AI that can be steered by input data is a liability. The system prompt is a constant string in `drafter.prompt.ts`. No interpolation. No exceptions.

**Tradeoff:** System behavior cannot be customized per-request. This is intentional. The anti-fabrication rule ("EVIDENCE_MISSING: [topic]") lives in the system prompt where it cannot be overridden by user content.

---

## ADR-005: Conservative confidence thresholds

**Why:** 0.85 auto-approve, 0.75 SME review, below 0.75 legal review, zero confidence means manual. These thresholds are conservative by design. A compliance AI that auto-approves borderline responses creates attestation risk — you're signing your name to a claim that's only partially supported by evidence. Better to route to human review than to confidently attest to something that's 70% grounded. Over time, as the RAG corpus grows and retrieval quality improves, more responses will naturally reach auto-approve. The system gets better without lowering the bar.

**Tradeoff:** Most responses will require human review initially. This is correct behavior for a compliance system. The AI drafts, humans approve. The value is in the drafting speed (minutes vs hours), not in removing humans from the loop.

---

## ADR-006: Test Once Comply Many — control ID design

**Why:** A single Okta MFA enforcement record maps to SOC2-CC6.1, ISO27001-A.9.4.2, and PCIDSS-REQ8.3.2 simultaneously. The normalizer does this mapping — not the connector. Connectors are dumb data retrievers. Business logic lives in the normalizer and the `test_once_ids` field in the control library. This separation means adding a new framework (say, HIPAA) requires updating the seed data mappings, not rewriting any connector code.

**Tradeoff:** Cross-framework mappings are maintained as data, not code. This requires domain expertise to set up correctly (you need to know that SOC2-CC6.1 and ISO27001-A.9.2.3 are equivalent). But that's a one-time setup cost, not an ongoing engineering cost.

---

## ADR-007: Connector-per-source over a generic connector

**Why:** Each source system (GitHub, Okta, AWS) has different authentication schemes, pagination patterns, rate limiting behavior, and data models. A "generic connector" with config-driven behavior would require an abstraction layer complex enough to handle all these differences — at which point you've just built a worse version of the specific connectors. One class per source means each connector is independently testable, independently deployable, and independently readable. A new engineer can understand the Okta connector without understanding GitHub's Link header pagination.

**Tradeoff:** Some code duplication in the `httpGet` retry logic across connectors. Mitigated by the shared pattern (identical structure in each). If this became a real maintenance burden, we'd extract a shared HTTP client — but with 3 connectors, that's premature abstraction.

---

## ADR-008: nock over real HTTP in tests — zero network calls

**Why:** Tests must be runnable with no network access, no real credentials, and no external service dependencies. A test suite that requires a valid `GITHUB_TOKEN` to run is a test suite that breaks when someone rotates credentials, runs in a sandboxed CI environment, or works on an airplane. Every test in this repo runs in <6 seconds with zero network calls. The tradeoff: connectors use `node:https` instead of native `fetch()` because nock v13 intercepts at the `http.ClientRequest` level. Minor ergonomic cost for complete test isolation.

**Tradeoff:** We're testing against mocked responses, not real APIs. Integration tests against real systems would catch API contract changes. That's a separate concern — handled by health checks in production, not by the unit test suite.
