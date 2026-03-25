# Operations Runbook

## Incident Response Procedures

### When a connector fails

**Symptoms:** Evidence collection job fails, `connector:run` exits non-zero, health check returns false.

**Steps:**
1. Check the connector health: `pnpm --filter evidence-engine run connector:run --source=<name>`
2. Check logs for specific error type:
   - `AuthError` → credentials expired or revoked. Rotate immediately (see Credential Rotation below).
   - `RateLimitError` → source API rate limit hit. Check if collection schedule is too aggressive.
   - `ConnectorError` / 5xx → source API is down. Wait and retry. If persistent >1 hour, open incident.
3. Check the source system's status page (e.g., status.github.com, status.okta.com, health.aws.amazon.com).
4. If the connector was working and suddenly stopped, check if the source system changed their API (version, endpoints, auth scheme).
5. Verify env vars are set: `echo $GITHUB_TOKEN | head -c 10` (check prefix, not full value).

**Escalation:** If unresolved after 30 minutes, page Security Engineering on-call.

### When confidence scores drop suddenly

**Symptoms:** Control confidence drops from pass (1.0) to drift (0.5) or fail (0.0) without a known change.

**Steps:**
1. Query recent evidence: `SELECT * FROM control_evidence WHERE control_id = '<id>' ORDER BY collected_at DESC LIMIT 5;`
2. Check if the drop correlates with a connector failure (stale evidence = stale confidence).
3. Check if the source system configuration actually changed:
   - GitHub: Were branch protection rules modified? Run connector manually and compare.
   - Okta: Was MFA policy disabled? Check Okta admin console.
   - AWS: Did a Config rule go NON_COMPLIANT? Check AWS Config dashboard.
4. If the drop is legitimate (real configuration change), the drift event will have been created automatically.
5. Check if a Jira ticket was auto-created: `SELECT jira_ticket_id FROM control_drift_events WHERE control_id = '<id>' ORDER BY detected_at DESC LIMIT 1;`

**Escalation:** If confidence drop is unexplained, escalate to the control owner listed in the control library.

### When a critical PCI control goes to fail status

**This is a P1 incident.** PCI controls at Tier 1 that fail indicate a potential compliance gap in the cardholder data environment.

**Immediate steps (within 15 minutes):**
1. Verify the failure is real (not a connector bug) by manually checking the source system.
2. A Slack alert should have been posted to `#security-alerts`. If not, post manually.
3. A Jira P1 ticket should have been auto-created. If not, create one manually.
4. Notify the CISO and the PCI compliance team.
5. Document the failure timeline: when it started, when it was detected, when remediation began.

**Remediation:**
- For PCIDSS-REQ1.2 (network segmentation): Check VPC security groups immediately. Revert any recent changes.
- For PCIDSS-REQ3.4 (PAN encryption): Verify S3 encryption is enabled. Check Stripe integration is token-only.
- For PCIDSS-REQ6.3 (secure development): Verify branch protection rules on all repos.
- For PCIDSS-REQ7.1 (access restriction): Audit admin roles in Okta immediately.
- For PCIDSS-REQ10.1 (audit trails): Verify CloudTrail is enabled and delivering logs.

**Post-incident:**
- Update the drift event with `resolved_at` timestamp.
- Conduct root cause analysis within 48 hours.
- Document in the PCI compliance log.

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
9. Run `pnpm build && pnpm test` — all tests must pass.

### How to add a new control to the library

1. Add the INSERT statement to `db/seed/control-library.sql` with `ON CONFLICT` handling.
2. Choose the correct framework enum: `SOC2`, `ISO27001`, `PCIDSS`, `ISO42001`.
3. Assign a tier: 1 (critical), 2 (high), 3 (medium).
4. Map `test_once_ids` to equivalent controls in other frameworks.
5. Run `pnpm db:seed` to insert.
6. Update `compliance/control-library.md` with the new control documentation.
7. Update the relevant connector's `MAPPED_CONTROLS` if evidence collection applies.

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
4. Verify retrieval: query for a relevant question and check that the new document chunks appear in results.

**Important:** After ingesting, the IVFFlat index may need rebuilding if you've added a significant number of chunks (>10% of existing corpus): `REINDEX INDEX idx_chunks_embedding;`

### Credential rotation schedule

| Credential | Rotation frequency | Owner | Notes |
|-----------|-------------------|-------|-------|
| GITHUB_TOKEN | 90 days | Security Engineering | Use fine-grained PAT with org:read, repo scope |
| OKTA_TOKEN | 90 days | IT Operations | API token from Okta admin console |
| AWS_ACCESS_KEY_ID / SECRET | 90 days | Infrastructure Engineering | Use IAM user with read-only Config + CloudTrail |
| ANTHROPIC_API_KEY | 180 days | AI Engineering | Monitor usage dashboard for anomalies |
| VOYAGE_API_KEY | 180 days | AI Engineering | Lower rotation frequency — embeddings are idempotent |
| JIRA_API_TOKEN | 90 days | IT Operations | Service account, not personal |
| SLACK_WEBHOOK_URL | Annual | Security Engineering | Rotate if channel is recreated |
| STRIPE_SECRET_KEY | 90 days | Payments Engineering | Read-only key for evidence collection only |
| ANECDOTES_API_KEY | 90 days | Compliance team | Coordinate with Anecdotes support |

**Rotation procedure:**
1. Generate new credential in the source system.
2. Update the env var in the deployment environment (not `.env` — use secrets manager).
3. Run the connector health check to verify: `pnpm --filter evidence-engine run connector:run --source=<name>`
4. Revoke the old credential only after confirming the new one works.
5. Log the rotation in the compliance audit trail.

### Monthly operational checklist

- [ ] Review all control confidence scores — investigate any below 0.8
- [ ] Review drift events from the past month — ensure all have `resolved_at` timestamps
- [ ] Verify all connectors are healthy: run health checks across all sources
- [ ] Review auto-drafted questionnaire responses in the review queue
- [ ] Check disk usage on Postgres (evidence table grows continuously)
- [ ] Verify RAG retrieval quality: test 5 sample questions against the pipeline
- [ ] Review Slack alert volume — too many alerts indicates noisy controls
- [ ] Archive resolved Jira tickets and update MTTR metrics

### Quarterly operational checklist

- [ ] Rotate all credentials due this quarter (see schedule above)
- [ ] Run full evidence collection across all connectors and verify completeness
- [ ] Review and update the control library for any new regulatory requirements
- [ ] Conduct access review: verify connector service accounts have minimum permissions
- [ ] Review and update OPA policies for any new infrastructure patterns
- [ ] Generate board scorecard: overall compliance posture, trends, gap summary
- [ ] Update PCI scoping document if architecture has changed
- [ ] Test disaster recovery: verify evidence can be re-collected from scratch
- [ ] Review AI drafter accuracy: sample 20 recent drafts and grade quality
- [ ] Update this runbook with any new procedures discovered during the quarter
