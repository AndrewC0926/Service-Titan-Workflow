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
- **Evidence source:** Okta (admin roles, privileged access inventory)
- **Collection frequency:** Every 6 hours
- **Control owner:** Security Engineering
- **Cross-framework:** SOC2-CC6.1, PCIDSS-REQ7.1

### ISO27001-A.9.2.5 — Review of User Access Rights
- **Tier:** 2 (High)
- **Evidence source:** Okta (access review automation, inactive user detection)
- **Collection frequency:** Every 6 hours
- **Control owner:** IT Operations
- **Cross-framework:** SOC2-CC6.2, PCIDSS-REQ8.3.2

### ISO27001-A.9.4.2 — Secure Log-on Procedures
- **Tier:** 1 (Critical)
- **Evidence source:** Okta (MFA policy, session management)
- **Collection frequency:** Every 6 hours
- **Control owner:** Security Engineering
- **Cross-framework:** SOC2-CC6.3, PCIDSS-REQ8.3.9

### ISO27001-A.10.1.1 — Policy on Use of Cryptographic Controls
- **Tier:** 2 (High)
- **Evidence source:** AWS Config (S3 encryption, RDS encryption, TLS configuration)
- **Collection frequency:** Every 30 minutes
- **Control owner:** Infrastructure Engineering
- **Cross-framework:** SOC2-CC6.6, PCIDSS-REQ1.2, PCIDSS-REQ3.4

### ISO27001-A.12.2.1 — Controls Against Malware
- **Tier:** 2 (High)
- **Evidence source:** AWS Config; CrowdStrike (future)
- **Collection frequency:** Every 30 minutes
- **Control owner:** Security Engineering
- **Cross-framework:** SOC2-CC6.7, PCIDSS-REQ5.1

### ISO27001-A.12.4.1 — Event Logging
- **Tier:** 1 (Critical)
- **Evidence source:** AWS CloudTrail (trail enabled, log delivery)
- **Collection frequency:** Every 30 minutes
- **Control owner:** Security Operations
- **Cross-framework:** SOC2-CC7.2, PCIDSS-REQ10.1

### ISO27001-A.12.4.3 — Administrator and Operator Logs
- **Tier:** 1 (Critical)
- **Evidence source:** AWS CloudTrail (privileged API events)
- **Collection frequency:** Every 30 minutes
- **Control owner:** Security Operations
- **Cross-framework:** SOC2-CC7.2

### ISO27001-A.12.6.1 — Management of Technical Vulnerabilities
- **Tier:** 2 (High)
- **Evidence source:** GitHub (Dependabot, secret scanning)
- **Collection frequency:** Every 4 hours
- **Control owner:** Security Engineering
- **Cross-framework:** SOC2-CC6.8, PCIDSS-REQ11.3

### ISO27001-A.14.2.2 — System Change Control Procedures
- **Tier:** 1 (Critical)
- **Evidence source:** GitHub (branch protection, PR enforcement)
- **Collection frequency:** Every 4 hours
- **Control owner:** Engineering Management
- **Cross-framework:** SOC2-CC8.1, PCIDSS-REQ6.3

### ISO27001-A.16.1.1 — Responsibilities and Procedures for Incident Management
- **Tier:** 1 (Critical)
- **Evidence source:** AWS CloudTrail, Okta session logs, Jira (incident tickets)
- **Collection frequency:** Every 30 minutes
- **Control owner:** Security Operations
- **Cross-framework:** SOC2-CC7.2, PCIDSS-REQ10.1

## PCI DSS 4.0 Requirements

### PCIDSS-REQ1.2 — Restrict Connections Between Untrusted Networks
- **Tier:** 1 (Critical)
- **Evidence source:** AWS Config (security groups, VPC flow logs)
- **Collection frequency:** Every 30 minutes
- **Control owner:** Infrastructure Engineering
- **Cross-framework:** SOC2-CC6.6, ISO27001-A.10.1.1

### PCIDSS-REQ3.4 — Render PAN Unreadable
- **Tier:** 1 (Critical)
- **Evidence source:** AWS Config (S3 encryption); architecture review (Stripe token-only)
- **Collection frequency:** Every 30 minutes + annual architecture review
- **Control owner:** Security Engineering
- **Cross-framework:** ISO27001-A.10.1.1

### PCIDSS-REQ5.1 — Deploy Anti-Virus on Susceptible Systems
- **Tier:** 2 (High)
- **Evidence source:** CrowdStrike (future connector)
- **Collection frequency:** Planned: every 4 hours
- **Control owner:** Security Engineering
- **Cross-framework:** SOC2-CC6.7, ISO27001-A.12.2.1

### PCIDSS-REQ6.3 — Develop Secure Software Applications
- **Tier:** 1 (Critical)
- **Evidence source:** GitHub (branch protection, PR reviews, CODEOWNERS)
- **Collection frequency:** Every 4 hours
- **Control owner:** Engineering Management
- **Cross-framework:** SOC2-CC8.1, ISO27001-A.14.2.2

### PCIDSS-REQ7.1 — Limit Access to System Components
- **Tier:** 1 (Critical)
- **Evidence source:** Okta (RBAC, admin role inventory)
- **Collection frequency:** Every 6 hours
- **Control owner:** Security Engineering
- **Cross-framework:** SOC2-CC6.1, ISO27001-A.9.2.3

### PCIDSS-REQ8.3.2 — Strong Password Requirements
- **Tier:** 2 (High)
- **Evidence source:** Okta (password policy configuration)
- **Collection frequency:** Every 6 hours
- **Control owner:** IT Operations
- **Cross-framework:** SOC2-CC6.2, ISO27001-A.9.2.5

### PCIDSS-REQ8.3.9 — Multi-Factor Authentication for Remote Access
- **Tier:** 1 (Critical)
- **Evidence source:** Okta (MFA policy, enrollment rate)
- **Collection frequency:** Every 6 hours
- **Control owner:** Security Engineering
- **Cross-framework:** SOC2-CC6.3, ISO27001-A.9.4.2

### PCIDSS-REQ10.1 — Audit Trails for System Components
- **Tier:** 1 (Critical)
- **Evidence source:** AWS CloudTrail, Okta session logs
- **Collection frequency:** Every 30 minutes
- **Control owner:** Security Operations
- **Cross-framework:** SOC2-CC7.2, SOC2-CC7.3, ISO27001-A.16.1.1

### PCIDSS-REQ11.3 — Penetration Testing
- **Tier:** 2 (High)
- **Evidence source:** Manual (pen test reports); HackerOne (future)
- **Collection frequency:** Annual + after significant changes
- **Control owner:** Security Engineering
- **Cross-framework:** SOC2-CC6.8, ISO27001-A.12.6.1

## ISO 42001 AI Management System

### ISO42001-6.1.2 — AI Risk Assessment
- **Tier:** 1 (Critical)
- **Evidence source:** AI Registry (auto-discovered workloads, risk assessments)
- **Collection frequency:** On system registration + quarterly review
- **Control owner:** AI Governance Lead
- **Cross-framework:** None (AI-specific)

### ISO42001-8.4.1 — AI System Documentation
- **Tier:** 2 (High)
- **Evidence source:** AI Registry (system metadata, data inputs, training sources)
- **Collection frequency:** On system registration + on change
- **Control owner:** AI Governance Lead
- **Cross-framework:** None (AI-specific)

### ISO42001-9.1.1 — AI Performance Monitoring
- **Tier:** 2 (High)
- **Evidence source:** AI Registry (performance metrics, monitoring config)
- **Collection frequency:** Continuous (per-system)
- **Control owner:** ML Engineering
- **Cross-framework:** None (AI-specific)

### ISO42001-10.1.1 — AI Continual Improvement
- **Tier:** 3 (Medium)
- **Evidence source:** AI Registry (review history, improvement actions)
- **Collection frequency:** Quarterly
- **Control owner:** AI Governance Lead
- **Cross-framework:** None (AI-specific)
