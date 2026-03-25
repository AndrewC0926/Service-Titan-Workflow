# Control Library Reference

33 controls across 4 frameworks. Cross-framework mappings enable Test Once Comply Many (TOCM) — a single evidence artifact satisfies equivalent controls across frameworks.

## SOC 2 Trust Services Criteria

### SOC2-CC6.1 — Logical and Physical Access Controls
- **Tier:** 1 (Critical)
- **Requirement:** Logical access security over protected information assets
- **Evidence source:** Okta (MFA enrollment, RBAC, admin roles)
- **Collection frequency:** Every 6 hours
- **Control owner:** Security Engineering
- **Cross-framework:** ISO27001-A.9.2.3, PCIDSS-REQ7.1
- **Audit notes:** Auditors expect 100% MFA enrollment for production access. Okta connector tracks enrollment rate and flags drift below 95%.

### SOC2-CC6.2 — User Authentication and Password Policy
- **Tier:** 1 (Critical)
- **Requirement:** Registration and authorization of new users before issuing credentials
- **Evidence source:** Okta (user provisioning, inactive accounts, access reviews)
- **Collection frequency:** Every 6 hours
- **Control owner:** IT Operations
- **Cross-framework:** ISO27001-A.9.2.5, PCIDSS-REQ8.3.2
- **Audit notes:** Watch for inactive accounts (>90 days no login). Quarterly access reviews must be documented.

### SOC2-CC6.3 — Multi-Factor Authentication
- **Tier:** 1 (Critical)
- **Requirement:** MFA required for system access
- **Evidence source:** Okta (MFA policy enforcement, enrollment statistics)
- **Collection frequency:** Every 6 hours
- **Control owner:** Security Engineering
- **Cross-framework:** ISO27001-A.9.4.2, PCIDSS-REQ8.3.9
- **Audit notes:** Policy must be in ACTIVE state. Any unenrolled users are flagged as drift.

### SOC2-CC6.6 — Network Security and Boundary Protection
- **Tier:** 2 (High)
- **Requirement:** Controls to prevent unauthorized network access
- **Evidence source:** AWS Config (security groups, VPC flow logs)
- **Collection frequency:** Every 30 minutes
- **Control owner:** Infrastructure Engineering
- **Cross-framework:** ISO27001-A.10.1.1, PCIDSS-REQ1.2
- **Audit notes:** RESTRICTED_SSH Config rule is critical. Any open SSH to 0.0.0.0/0 is immediate P1.

### SOC2-CC6.7 — Malware Detection and Prevention
- **Tier:** 2 (High)
- **Requirement:** Anti-malware technologies deployed
- **Evidence source:** AWS Config (S3 encryption, RDS encryption); CrowdStrike (future)
- **Collection frequency:** Every 30 minutes
- **Control owner:** Security Engineering
- **Cross-framework:** ISO27001-A.12.2.1, PCIDSS-REQ5.1
- **Audit notes:** S3 encryption is a critical Config rule. Currently maps to data-at-rest protection; endpoint detection via CrowdStrike connector planned.

### SOC2-CC6.8 — Vulnerability Management
- **Tier:** 2 (High)
- **Requirement:** Assess and manage vulnerabilities in infrastructure and software
- **Evidence source:** GitHub (Dependabot alerts, secret scanning); future: Snyk connector
- **Collection frequency:** Every 4 hours
- **Control owner:** Security Engineering
- **Cross-framework:** ISO27001-A.12.6.1, PCIDSS-REQ11.3
- **Audit notes:** Track time-to-remediation for critical CVEs. Target: <72 hours for critical, <30 days for high.

### SOC2-CC7.2 — Security Incident Detection and Response
- **Tier:** 1 (Critical)
- **Requirement:** Monitor for and respond to security incidents
- **Evidence source:** AWS CloudTrail (privileged API events); Okta (session logs)
- **Collection frequency:** Every 30 minutes (AWS), every 6 hours (Okta)
- **Control owner:** Security Operations
- **Cross-framework:** ISO27001-A.16.1.1, PCIDSS-REQ10.1
- **Audit notes:** CloudTrail must be enabled in all regions. Privileged events (DeleteTrail, StopLogging) trigger immediate P1 alert.

### SOC2-CC7.3 — Security Event Monitoring
- **Tier:** 2 (High)
- **Requirement:** Monitor for indicators of compromise
- **Evidence source:** AWS CloudTrail, application logs
- **Collection frequency:** Every 30 minutes
- **Control owner:** Security Operations
- **Cross-framework:** PCIDSS-REQ10.1
- **Audit notes:** Verify log retention >= 1 year (accessible), >= 3 months (immediately available).

### SOC2-CC8.1 — Change Management
- **Tier:** 1 (Critical)
- **Requirement:** Authorize, design, test, approve, and implement changes
- **Evidence source:** GitHub (branch protection, PR approval enforcement, CODEOWNERS)
- **Collection frequency:** Every 4 hours
- **Control owner:** Engineering Management
- **Cross-framework:** ISO27001-A.14.2.2, PCIDSS-REQ6.3
- **Audit notes:** All default branches must have protection enabled + required reviews. CODEOWNERS for critical paths (infra, payment, auth).

## ISO 27001 Annex A Controls

### ISO27001-A.9.2.3 — Management of Privileged Access Rights
- **Tier:** 1 (Critical)
- **Requirement:** The allocation and use of privileged access rights shall be restricted and controlled
- **Evidence source:** Okta (admin roles, privileged access inventory)
- **Collection frequency:** Every 6 hours
- **Control owner:** Security Engineering
- **Cross-framework:** SOC2-CC6.1, PCIDSS-REQ7.1
- **Audit notes:** Maintain a complete inventory of users with SUPER_ADMIN or ORG_ADMIN roles. Auditors will ask for justification for each privileged account. Target: <5% of total users hold admin roles. Any new admin assignment should trigger a Jira approval ticket.

### ISO27001-A.9.2.5 — Review of User Access Rights
- **Tier:** 2 (High)
- **Requirement:** Asset owners shall review users' access rights at regular intervals
- **Evidence source:** Okta (access review automation, inactive user detection)
- **Collection frequency:** Every 6 hours
- **Control owner:** IT Operations
- **Cross-framework:** SOC2-CC6.2, PCIDSS-REQ8.3.2
- **Audit notes:** Quarterly access reviews are mandatory. The Okta connector flags inactive accounts (>90 days no login). Auditors expect documented evidence that reviews were performed and that revocations were actioned. Keep the Jira ticket trail.

### ISO27001-A.9.4.2 — Secure Log-on Procedures
- **Tier:** 1 (Critical)
- **Requirement:** Access to systems shall be controlled by a secure log-on procedure
- **Evidence source:** Okta (MFA policy, session management)
- **Collection frequency:** Every 6 hours
- **Control owner:** Security Engineering
- **Cross-framework:** SOC2-CC6.3, PCIDSS-REQ8.3.9
- **Audit notes:** MFA policy must be ACTIVE with enrollment set to REQUIRED, not OPTIONAL. Session timeout must be <=12 hours for standard users, <=1 hour for admin sessions. The connector checks policy status — if it flips to INACTIVE, confidence drops to 0.0 immediately.

### ISO27001-A.10.1.1 — Policy on Use of Cryptographic Controls
- **Tier:** 2 (High)
- **Requirement:** A policy on the use of cryptographic controls for protection of information shall be developed and implemented
- **Evidence source:** AWS Config (S3 encryption, RDS encryption, TLS configuration)
- **Collection frequency:** Every 30 minutes
- **Control owner:** Infrastructure Engineering
- **Cross-framework:** SOC2-CC6.6, PCIDSS-REQ1.2, PCIDSS-REQ3.4
- **Audit notes:** All S3 buckets must have SSE enabled (AES256 or aws:kms). All RDS instances must have storage encryption enabled. TLS 1.2+ enforced on all ALB listeners. The OPA policy `encryption.rego` catches unencrypted S3 buckets at PR time — this is defense in depth on top of Config monitoring.

### ISO27001-A.12.2.1 — Controls Against Malware
- **Tier:** 2 (High)
- **Requirement:** Detection, prevention, and recovery controls to protect against malware shall be implemented
- **Evidence source:** AWS Config; CrowdStrike (future)
- **Collection frequency:** Every 30 minutes
- **Control owner:** Security Engineering
- **Cross-framework:** SOC2-CC6.7, PCIDSS-REQ5.1
- **Audit notes:** Currently evidenced through infrastructure-level controls (encrypted storage, network segmentation). Endpoint detection coverage via CrowdStrike connector is planned. Auditors may accept compensating controls if endpoint agent coverage is documented separately.

### ISO27001-A.12.4.1 — Event Logging
- **Tier:** 1 (Critical)
- **Requirement:** Event logs recording user activities, exceptions, faults, and information security events shall be produced, kept, and regularly reviewed
- **Evidence source:** AWS CloudTrail (trail enabled, log delivery)
- **Collection frequency:** Every 30 minutes
- **Control owner:** Security Operations
- **Cross-framework:** SOC2-CC7.2, PCIDSS-REQ10.1
- **Audit notes:** CloudTrail must be enabled in ALL regions, not just the primary. The AWS connector checks `CLOUD_TRAIL_ENABLED` Config rule — this is a critical rule, any NON_COMPLIANT result drops confidence to 0.0. Log delivery to S3 must be verified — a trail that's "enabled" but not delivering is a false positive.

### ISO27001-A.12.4.3 — Administrator and Operator Logs
- **Tier:** 1 (Critical)
- **Requirement:** System administrator and system operator activities shall be logged and the logs protected and regularly reviewed
- **Evidence source:** AWS CloudTrail (privileged API events)
- **Collection frequency:** Every 30 minutes
- **Control owner:** Security Operations
- **Cross-framework:** SOC2-CC7.2
- **Audit notes:** The AWS connector watches for DeleteTrail, StopLogging, and PutBucketPolicy events — these are the "someone is covering their tracks" signals. Any occurrence triggers an immediate P1 alert. Auditors will ask for evidence of log integrity — CloudTrail log file validation must be enabled.

### ISO27001-A.12.6.1 — Management of Technical Vulnerabilities
- **Tier:** 2 (High)
- **Requirement:** Information about technical vulnerabilities shall be obtained, evaluated, and appropriate measures taken
- **Evidence source:** GitHub (Dependabot, secret scanning)
- **Collection frequency:** Every 4 hours
- **Control owner:** Security Engineering
- **Cross-framework:** SOC2-CC6.8, PCIDSS-REQ11.3
- **Audit notes:** Track mean time to remediation (MTTR) for vulnerabilities by severity. Targets: critical <72 hours, high <30 days. Auditors expect a documented process for triaging and patching. Secret scanning alerts must be resolved immediately — an exposed secret is a P1.

### ISO27001-A.14.2.2 — System Change Control Procedures
- **Tier:** 1 (Critical)
- **Requirement:** Changes to systems within the development lifecycle shall be controlled by formal change control procedures
- **Evidence source:** GitHub (branch protection, PR enforcement)
- **Collection frequency:** Every 4 hours
- **Control owner:** Engineering Management
- **Cross-framework:** SOC2-CC8.1, PCIDSS-REQ6.3
- **Audit notes:** Every repo must have branch protection on default branch with required reviews >=1. CODEOWNERS file must exist for critical paths (infrastructure, payment, authentication). The GitHub connector checks all of these — confidence is 1.0 only when all repos are fully protected. Any repo without protection drops to 0.5.

### ISO27001-A.16.1.1 — Responsibilities and Procedures for Incident Management
- **Tier:** 1 (Critical)
- **Requirement:** Management responsibilities and procedures shall be established for security incident response
- **Evidence source:** AWS CloudTrail, Okta session logs, Jira (incident tickets)
- **Collection frequency:** Every 30 minutes
- **Control owner:** Security Operations
- **Cross-framework:** SOC2-CC7.2, PCIDSS-REQ10.1
- **Audit notes:** Auditors want to see documented incident response procedures AND evidence they've been followed. The alert router automatically creates Jira tickets for drift events — these serve as incident records. Keep the RUNBOOK.md up to date — auditors may ask to see it. Track MTTR from the dashboard scorecard endpoint.

## PCI DSS 4.0 Requirements

### PCIDSS-REQ1.2 — Restrict Connections Between Untrusted Networks
- **Tier:** 1 (Critical)
- **Requirement:** Restrict inbound and outbound traffic to that which is necessary for the cardholder data environment
- **Evidence source:** AWS Config (security groups, VPC flow logs)
- **Collection frequency:** Every 30 minutes
- **Control owner:** Infrastructure Engineering
- **Cross-framework:** SOC2-CC6.6, ISO27001-A.10.1.1
- **Audit notes:** The QSA will want to see network diagrams showing CDE segmentation. The AWS connector checks `RESTRICTED_SSH` and `VPC_FLOW_LOGS_ENABLED` Config rules. Any security group allowing 0.0.0.0/0 inbound on SSH (port 22) is an immediate failure. VPC flow logs must be enabled on all subnets — not just the CDE. See `docs/PCI-SCOPING.md` for the full network segmentation diagram.

### PCIDSS-REQ3.4 — Render PAN Unreadable
- **Tier:** 1 (Critical)
- **Requirement:** Render PAN unreadable anywhere it is stored using strong cryptography
- **Evidence source:** AWS Config (S3 encryption); architecture review (Stripe token-only)
- **Collection frequency:** Every 30 minutes + annual architecture review
- **Control owner:** Security Engineering
- **Cross-framework:** ISO27001-A.10.1.1
- **Audit notes:** Our primary defense is architectural: we never store PAN. Stripe Elements handles card entry, we store only token references (tok_xxx, pm_xxx). The QSA needs to verify this claim — point them to the payment flow in `docs/PCI-SCOPING.md`. The S3 encryption check is defense-in-depth for any data at rest. The OPA policy `encryption.rego` enforces this at PR time.

### PCIDSS-REQ5.1 — Deploy Anti-Virus on Susceptible Systems
- **Tier:** 2 (High)
- **Requirement:** Deploy anti-virus software on all systems commonly affected by malicious software
- **Evidence source:** CrowdStrike (future connector)
- **Collection frequency:** Planned: every 4 hours
- **Control owner:** Security Engineering
- **Cross-framework:** SOC2-CC6.7, ISO27001-A.12.2.1
- **Audit notes:** Currently a gap in automated evidence collection. CrowdStrike Falcon is deployed but the connector isn't built yet. For now, provide manual evidence: CrowdStrike dashboard screenshots showing agent coverage >=98% of endpoints. Compensating control: all workloads run in containers with read-only root filesystems.

### PCIDSS-REQ6.3 — Develop Secure Software Applications
- **Tier:** 1 (Critical)
- **Requirement:** Develop internal and external software applications securely following industry standards
- **Evidence source:** GitHub (branch protection, PR reviews, CODEOWNERS)
- **Collection frequency:** Every 4 hours
- **Control owner:** Engineering Management
- **Cross-framework:** SOC2-CC8.1, ISO27001-A.14.2.2
- **Audit notes:** DSS 4.0 requirement 6.4.3 specifically requires payment page script management — see `docs/PCI-SCOPING.md` for our CSP + SRI implementation. The GitHub connector verifies that all repos have branch protection and required reviews. CODEOWNERS must cover payment-related paths. The compliance gate (OPA) runs on every PR affecting infrastructure.

### PCIDSS-REQ7.1 — Limit Access to System Components
- **Tier:** 1 (Critical)
- **Requirement:** Limit access to system components and cardholder data to only those individuals whose job requires such access
- **Evidence source:** Okta (RBAC, admin role inventory)
- **Collection frequency:** Every 6 hours
- **Control owner:** Security Engineering
- **Cross-framework:** SOC2-CC6.1, ISO27001-A.9.2.3
- **Audit notes:** The QSA will ask for a matrix of roles to access levels. The Okta connector inventories all admin roles and flags any new assignments. Principle of least privilege: no shared accounts, no standing admin access. Target: 100% of production access goes through Okta SSO with role-based policies.

### PCIDSS-REQ8.3.2 — Strong Password Requirements
- **Tier:** 2 (High)
- **Requirement:** Require minimum password complexity for user accounts
- **Evidence source:** Okta (password policy configuration)
- **Collection frequency:** Every 6 hours
- **Control owner:** IT Operations
- **Cross-framework:** SOC2-CC6.2, ISO27001-A.9.2.5
- **Audit notes:** Okta password policy must enforce: minimum 12 characters, complexity requirements, password history >=4, lockout after 6 failed attempts. The connector checks policy configuration state. DSS 4.0 allows passphrase-based authentication as an alternative to complexity — document which approach is used.

### PCIDSS-REQ8.3.9 — Multi-Factor Authentication for Remote Access
- **Tier:** 1 (Critical)
- **Requirement:** MFA is implemented for all remote network access originating from outside the entity's network
- **Evidence source:** Okta (MFA policy, enrollment rate)
- **Collection frequency:** Every 6 hours
- **Control owner:** Security Engineering
- **Cross-framework:** SOC2-CC6.3, ISO27001-A.9.4.2
- **Audit notes:** 100% MFA enrollment is required for pass. The Okta connector tracks enrollment rate — any unenrolled active user drops confidence. Policy must be set to REQUIRED, not OPTIONAL. The QSA will specifically verify MFA for VPN access, SSH access, and admin console access. All three must be covered.

### PCIDSS-REQ10.1 — Audit Trails for System Components
- **Tier:** 1 (Critical)
- **Requirement:** Implement audit trails to link all access to system components to each individual user
- **Evidence source:** AWS CloudTrail, Okta session logs
- **Collection frequency:** Every 30 minutes
- **Control owner:** Security Operations
- **Cross-framework:** SOC2-CC7.2, SOC2-CC7.3, ISO27001-A.16.1.1
- **Audit notes:** CloudTrail trails must be enabled in all regions with log file validation turned on. Retention: logs must be immediately available for 3 months and retained for 1 year (DSS 4.0 requirement 10.7). The AWS connector verifies `CLOUD_TRAIL_ENABLED` — this is a critical rule. Okta session logs provide the identity layer: who logged in, from where, at what time.

### PCIDSS-REQ11.3 — Penetration Testing
- **Tier:** 2 (High)
- **Requirement:** Perform external and internal penetration testing regularly and after significant changes
- **Evidence source:** Manual (pen test reports); HackerOne (future)
- **Collection frequency:** Annual + after significant changes
- **Control owner:** Security Engineering
- **Cross-framework:** SOC2-CC6.8, ISO27001-A.12.6.1
- **Audit notes:** Annual pen test must cover both internal and external network. Must be performed by a qualified assessor (PCI SSC QSA or ASV-approved). After significant infrastructure changes (new subnet, new payment flow), a targeted re-test is required. Store pen test reports securely — they contain vulnerability details. Track remediation of findings to closure.

## ISO 42001 AI Management System

### ISO42001-6.1.2 — AI Risk Assessment
- **Tier:** 1 (Critical)
- **Requirement:** The organization shall identify and assess risks related to the development, deployment, and use of AI systems
- **Evidence source:** AI Registry (auto-discovered workloads, risk assessments)
- **Collection frequency:** On system registration + quarterly review
- **Control owner:** AI Governance Lead
- **Cross-framework:** None (AI-specific)
- **Audit notes:** Every AI system must have a documented risk assessment before deployment. The AI registry auto-discovers systems via GitHub code scanning (imports of ML/AI libraries). Risk tier assignment (low/medium/high/critical) must be justified in writing. The questionnaire auto-drafter itself is an AI system — it must be registered. Auditors will check that risk assessments are reviewed quarterly, not just created once.

### ISO42001-8.4.1 — AI System Documentation
- **Tier:** 2 (High)
- **Requirement:** The organization shall document AI system design, data requirements, and operational parameters
- **Evidence source:** AI Registry (system metadata, data inputs, training sources)
- **Collection frequency:** On system registration + on change
- **Control owner:** AI Governance Lead
- **Cross-framework:** None (AI-specific)
- **Audit notes:** Each AI system entry in the `ai_systems` table must have populated fields for: description, system_type, data_inputs, training_data_src, human_oversight, and output_scope. Empty fields are audit findings. The compliance engine's own drafter should document: model (claude-sonnet-4-20250514), data inputs (RAG corpus from policy documents), human oversight (4-tier review system), output scope (questionnaire responses only).

### ISO42001-9.1.1 — AI Performance Monitoring
- **Tier:** 2 (High)
- **Requirement:** The organization shall monitor, measure, and evaluate AI system performance against defined objectives
- **Evidence source:** AI Registry (performance metrics, monitoring config)
- **Collection frequency:** Continuous (per-system)
- **Control owner:** ML Engineering
- **Cross-framework:** None (AI-specific)
- **Audit notes:** For the questionnaire drafter: track confidence score distribution over time, flag rate (how often EVIDENCE_MISSING appears), review tier distribution (what % reaches auto_approve vs manual). If flag rate increases, the RAG corpus may need updating. If auto_approve rate drops, investigate whether the confidence calculation or the corpus quality has degraded. Dashboard scorecard provides the monitoring surface.

### ISO42001-10.1.1 — AI Continual Improvement
- **Tier:** 3 (Medium)
- **Requirement:** The organization shall continually improve the suitability, adequacy, and effectiveness of AI management
- **Evidence source:** AI Registry (review history, improvement actions)
- **Collection frequency:** Quarterly
- **Control owner:** AI Governance Lead
- **Cross-framework:** None (AI-specific)
- **Audit notes:** Quarterly review must document: what changed since last review, what improvements were made, what issues were identified. For the drafter: sample 20 responses per quarter, grade them against auditor expectations, feed corrections back into the RAG corpus. Track whether confidence thresholds need adjustment based on actual audit findings. This is the "close the loop" control — without it, the AI system improves only by accident.
