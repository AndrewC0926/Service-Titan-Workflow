# Operations Runbook

This document is the operating manual for the compliance engine. It answers: who runs this at 2am when something breaks, what do they do, and how do we stay audit-ready between audits — not just during them.

---

## Incident Response

### When a connector fails

**Symptoms:** Evidence collection job fails, `connector:run` exits non-zero, health check returns false. The `control_evidence` table stops receiving new rows for that source.

**Steps:**
1. Check the connector health: `pnpm --filter evidence-engine run connector:run --source=<name>`
2. Check logs for specific error type:
   - `AuthError` → credentials expired or revoked. Rotate immediately (see Credential Rotation below).
   - `RateLimitError` → source API rate limit hit. Check if collection schedule is too aggressive. Okta defaults to every 6 hours; GitHub every 4 hours. If the org has grown, consider reducing frequency.
   - `ConnectorError` / 5xx → source API is down. Wait and retry. If persistent >1 hour, open incident.
3. Check the source system's status page (status.github.com, status.okta.com, health.aws.amazon.com).
4. If the connector was working and suddenly stopped, check if the source system changed their API (version, endpoints, auth scheme).
5. Verify env vars are set: `echo $GITHUB_TOKEN | head -c 10` (check prefix, not full value — never log the full secret).

**Escalation:** If unresolved after 30 minutes, page the compliance infrastructure on-call (see On-Call Rotation below).

**Evidence impact:** While a connector is down, evidence for its mapped controls goes stale. The `expires_at` field on existing evidence will eventually pass, and those controls will show `stale` status. This is by design — stale is better than silently assuming compliance.

---

### When confidence scores drop suddenly

**Symptoms:** Control confidence drops from pass (1.0) to drift (0.5) or fail (0.0) without a known change. Dashboard posture score drops.

**Steps:**
1. Query recent evidence:
   ```sql
   SELECT control_id, status, confidence, collected_at, source
   FROM control_evidence
   WHERE control_id = '<id>'
   ORDER BY collected_at DESC LIMIT 5;
   ```
2. Check if the drop correlates with a connector failure (stale evidence = stale confidence).
3. Check if the source system configuration actually changed:
   - GitHub: Were branch protection rules modified? Run connector manually and compare.
   - Okta: Was MFA policy disabled or an admin role added? Check Okta admin console.
   - AWS: Did a Config rule go NON_COMPLIANT? Check AWS Config dashboard.
4. If the drop is legitimate (real configuration change), the drift event should have been created automatically:
   ```sql
   SELECT * FROM control_drift_events
   WHERE control_id = '<id>'
   ORDER BY detected_at DESC LIMIT 1;
   ```
5. Verify a Jira ticket was auto-created. If the alert router failed, create one manually.

**Escalation:** If the confidence drop is unexplained (no connector failure, no source system change), escalate to the control owner listed in `compliance/control-library.md`. This may indicate a bug in the connector's confidence calculation.

---

### When a critical PCI control goes to fail status

**This is a P1 incident.** PCI controls at Tier 1 that fail indicate a potential compliance gap in the cardholder data environment. The alert router should have already posted to `#security-alerts` and created a P1 Jira ticket.

**Immediate steps (within 15 minutes):**
1. Verify the failure is real (not a connector bug) by manually checking the source system.
2. Confirm the Slack alert was posted to `#security-alerts`. If not, post manually.
3. Confirm a Jira P1 ticket was auto-created. If not, create one manually.
4. Notify the CISO and the PCI compliance team.
5. Begin documenting the failure timeline: when it started, when it was detected, when remediation began.

**Control-specific remediation:**

| Control | What to check | Remediation |
|---------|--------------|-------------|
| PCIDSS-REQ1.2 (network segmentation) | VPC security groups, NACLs | Revert recent SG changes, verify no 0.0.0.0/0 rules |
| PCIDSS-REQ3.4 (PAN encryption) | S3 encryption, Stripe integration | Verify SSE enabled on all buckets, confirm token-only architecture |
| PCIDSS-REQ6.3 (secure development) | Branch protection rules | Re-enable required reviews on all default branches |
| PCIDSS-REQ7.1 (access restriction) | Okta admin roles | Audit and remove any unauthorized admin assignments |
| PCIDSS-REQ8.3.9 (MFA) | Okta MFA policy | Verify policy is ACTIVE, check enrollment rate |
| PCIDSS-REQ10.1 (audit trails) | CloudTrail status | Verify trails enabled in all regions, check log delivery |

**Post-incident (within 48 hours):**
1. Update the drift event with `resolved_at` timestamp.
2. Conduct root cause analysis. Document: what changed, who changed it, why it wasn't caught earlier.
3. If the failure was caused by a manual configuration change, add an OPA policy to prevent recurrence.
4. Document in the PCI compliance log for the next QSA review.

---

## On-Call Rotation

### Compliance infrastructure on-call

| Week | Primary | Secondary | Escalation |
|------|---------|-----------|------------|
| Week 1 | Security Engineering | Infrastructure Engineering | CISO |
| Week 2 | Infrastructure Engineering | Security Engineering | CISO |
| Week 3 | Security Engineering | AI Engineering | CISO |
| Week 4 | Infrastructure Engineering | Security Engineering | CISO |

**On-call responsibilities:**
- Monitor `#security-alerts` and `#compliance` Slack channels
- Acknowledge P1 alerts within 15 minutes, P2 within 1 hour
- Triage connector failures and escalate if unresolved within 30 minutes
- Ensure all PCI Tier 1 controls remain in `pass` status

**Handoff procedure:**
- Outgoing on-call reviews all open drift events and Jira tickets with incoming
- Confirm all connector health checks are passing at handoff time
- Document any ongoing issues in the #compliance-ops Slack channel

---

## Operational Procedures

### How to add a new source system connector

1. Create `packages/evidence-engine/src/connectors/<source>.connector.ts`
2. Implement the `Connector` interface from `@compliance-engine/types`:
   - `source: string` — unique identifier
   - `schedule: string` — cron expression
   - `collect(): Promise<RawEvidence[]>` — pull data from source API
   - `normalize(raw: RawEvidence[]): Promise<ControlEvidence[]>` — map to controls
   - `healthCheck(): Promise<boolean>` — verify connectivity
3. Write tests first in `__tests__/<source>.connector.test.ts` using nock (no real HTTP).
4. Follow the HTTP pattern from `github.connector.ts`: use `node:https`, handle 401/429/5xx.
5. Add the connector to `registry.ts` with its env var guards.
6. Add required env vars to `.env.example`.
7. Add the control ID mappings to `suggestedControlIds` matching the seed data.
8. Export from `packages/evidence-engine/src/index.ts`.
9. Run `pnpm build && pnpm test` — all tests must pass before merging.

### How to add a new control to the library

1. Add the INSERT statement to `db/seed/control-library.sql` with `ON CONFLICT` handling.
2. Choose the correct framework enum: `SOC2`, `ISO27001`, `PCIDSS`, `ISO42001`.
3. Assign a tier: 1 (critical), 2 (high), 3 (medium).
4. Map `test_once_ids` to equivalent controls in other frameworks.
5. Run `pnpm db:seed` to insert.
6. Update `compliance/control-library.md` with the new control documentation.
7. Update the relevant connector's `MAPPED_CONTROLS` if evidence collection applies.
8. If the control is PCI Tier 1, update the alert router's routing logic if needed.

### How to ingest new policy documents into the RAG pipeline

1. Prepare the document as plain text (PDF extraction should be done externally for now).
2. Call the ingest function:
   ```typescript
   import { ingestDocument } from '@compliance-engine/ai-layer';

   const result = await ingestDocument(documentText, {
     sourceDoc: 'policy-name.pdf',
     framework: 'SOC2', // or null for multi-framework
     controlIds: ['SOC2-CC6.1'], // relevant control IDs
   }, process.env.VOYAGE_API_KEY);
   ```
3. Write the resulting chunks to the database:
   ```sql
   INSERT INTO document_chunks (source_doc, framework, control_ids, chunk_index, text, embedding)
   VALUES ($1, $2, $3, $4, $5, $6::vector);
   ```
4. Verify retrieval: query for a relevant question and check that the new document chunks appear in results with reasonable similarity scores (>0.7).
5. After ingesting >10% new corpus, rebuild the IVFFlat index: `REINDEX INDEX idx_chunks_embedding;`

---

## Credential Rotation

| Credential | Rotation | Owner | Notes |
|-----------|----------|-------|-------|
| GITHUB_TOKEN | 90 days | Security Engineering | Fine-grained PAT, org:read + repo scope only |
| OKTA_TOKEN | 90 days | IT Operations | API token from Okta admin console, read-only |
| AWS_ACCESS_KEY_ID / SECRET | 90 days | Infrastructure Engineering | IAM user with read-only Config + CloudTrail |
| ANTHROPIC_API_KEY | 180 days | AI Engineering | Monitor usage dashboard for anomalies |
| VOYAGE_API_KEY | 180 days | AI Engineering | Embeddings are idempotent — lower rotation risk |
| JIRA_API_TOKEN | 90 days | IT Operations | Service account, not personal token |
| SLACK_WEBHOOK_URL | Annual | Security Engineering | Rotate if channel is recreated |
| STRIPE_SECRET_KEY | 90 days | Payments Engineering | Read-only key for evidence collection only |
| ANECDOTES_API_KEY | 90 days | Compliance team | Coordinate with Anecdotes support for rotation |

**Rotation procedure:**
1. Generate new credential in the source system.
2. Update the env var in the deployment environment (secrets manager, not `.env` file).
3. Run the connector health check: `pnpm --filter evidence-engine run connector:run --source=<name>`
4. Confirm evidence collection succeeds with the new credential.
5. Revoke the old credential only after confirming the new one works.
6. Log the rotation date and who performed it in the compliance audit trail.

---

## Continuous Audit-Readiness Cadence

### Monthly

- [ ] Review all control confidence scores — investigate any below 0.8
- [ ] Review drift events — ensure all have `resolved_at` timestamps or active Jira tickets
- [ ] Verify all connector health checks pass: run `healthCheck()` across all sources
- [ ] Review the questionnaire review queue — clear any stale drafts
- [ ] Check Postgres disk usage (evidence table grows continuously — plan retention)
- [ ] Verify RAG retrieval quality: test 5 sample compliance questions against the pipeline
- [ ] Review Slack alert volume — high volume indicates noisy controls that need threshold tuning
- [ ] Update MTTR metrics from resolved Jira tickets

### Quarterly

- [ ] Rotate all credentials due this quarter (see rotation schedule above)
- [ ] Run full evidence collection across all connectors and verify completeness
- [ ] Review confidence thresholds against actual audit findings — are we auto-approving things that auditors later questioned? Tighten thresholds if so
- [ ] Conduct access review: verify connector service accounts have minimum necessary permissions
- [ ] Review and update OPA policies for any new infrastructure patterns
- [ ] Generate board scorecard: `GET /api/dashboard/scorecard` — overall posture, critical gaps, trends
- [ ] Update PCI scoping document if architecture has changed
- [ ] Test disaster recovery: can evidence be re-collected from scratch if the DB is lost?
- [ ] Sample 20 AI-drafted questionnaire responses and grade accuracy — feed corrections back into RAG corpus
- [ ] Review this runbook and update with any new procedures discovered during the quarter

### Annually

- [ ] Rotate all credentials regardless of individual schedules (full rotation)
- [ ] Full control library review: are there new regulatory requirements? New frameworks to add?
- [ ] Review cross-framework `test_once_ids` mappings with compliance counsel
- [ ] Archive evidence older than retention policy (default: 3 years for PCI, 7 years for SOC2)
- [ ] Re-evaluate vendor dependencies: Voyage AI, Anthropic, Anecdotes — pricing, reliability, alternatives
- [ ] Review and update the PCI scoping document with the QSA
- [ ] Conduct tabletop exercise: simulate a PCI control failure and walk through the incident response procedure
- [ ] Update architecture documentation if any systems were added or removed
- [ ] Review AI drafter system prompt and anti-fabrication rules — are they still appropriate?
- [ ] Present annual compliance posture report to the board
