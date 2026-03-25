# PCI DSS 4.0 Scoping Document

## ServiceTitan Payments + Stripe Architecture

### Payment Flow

```
Customer Browser
    |
    v
ServiceTitan Frontend (React)
    |  Stripe.js / Elements — PAN entered directly into Stripe iframe
    |  No raw card data touches ServiceTitan servers
    v
Stripe API (tokenization)
    |
    v
ServiceTitan Backend (Node.js)
    |  Receives only Stripe token (tok_xxx) or PaymentIntent ID (pi_xxx)
    |  Stores token reference in PostgreSQL — never raw PAN
    v
Stripe (charge processing)
    |
    v
Payment processor / acquiring bank
```

### CDE Boundary Definition

**Cardholder Data Environment (CDE):** Stripe-hosted only. ServiceTitan systems are explicitly out of CDE scope because:

1. **No raw PAN storage.** Payment card numbers are entered into Stripe Elements iframes that are hosted on Stripe's domain. The PAN never transits ServiceTitan servers or networks.
2. **Token-only architecture.** ServiceTitan stores Stripe token references (`tok_xxx`, `pm_xxx`, `cus_xxx`) which are non-reversible — they cannot be used to reconstruct the original PAN.
3. **No PAN in logs.** Application logging excludes payment fields. Log aggregation rules (Datadog/Splunk) filter any pattern matching card number regex as a safety net.
4. **No PAN in backups.** Database backups contain token references only.

### SAQ Applicability

**Applicable SAQ: SAQ A**

Justification:
- All payment processing is fully outsourced to Stripe
- Card data is entered into Stripe-hosted iframes (Stripe Elements)
- No electronic storage, processing, or transmission of cardholder data on ServiceTitan systems
- ServiceTitan qualifies as an e-commerce merchant using a third-party hosted payment page

**Why not SAQ A-EP:**
- SAQ A-EP applies when the merchant website controls the page that redirects to the payment processor. With Stripe Elements embedded as an iframe, the payment form is Stripe-hosted, keeping us at SAQ A.

**Why not SAQ D:**
- SAQ D applies when the merchant stores, processes, or transmits cardholder data. Our token-only architecture explicitly prevents this.

### DSS 4.0 Specific Requirements

#### Requirement 6.4.3 — Payment Page Script Management

> All payment page scripts that are loaded and executed in the consumer's browser are managed as follows: a method is implemented to confirm that each script is authorized, the integrity of each script is assured, an inventory of all scripts is maintained with written justification.

**Implementation:**
- Stripe Elements loads from `js.stripe.com` via a `<script>` tag with Subresource Integrity (SRI) hash
- Content Security Policy (CSP) headers restrict script sources to `'self'` and `js.stripe.com`
- Automated CSP violation reporting to `#security-alerts` Slack channel
- Quarterly script inventory audit tracked in Jira (control: PCIDSS-REQ6.3)
- Evidence source: GitHub connector (CODEOWNERS for payment pages), AWS Config (CloudFront CSP headers)

#### Requirement 11.6.1 — Change and Tamper Detection on Payment Pages

> A change- and tamper-detection mechanism is deployed on payment pages as follows: alerting personnel to unauthorized modification, mechanism is configured to evaluate at least weekly.

**Implementation:**
- Integrity monitoring on payment page bundle hashes via CI/CD pipeline
- SHA-256 hash of production payment page JS bundle stored and compared on each deploy
- Any hash mismatch triggers P1 alert to `#security-alerts` and auto-creates Jira ticket
- Weekly automated scan comparing deployed bundle hash against known-good baseline
- Evidence source: GitHub connector (PR enforcement on payment page files), CloudTrail (deployment events)

### Evidence Source Mapping per PCI Requirement

| PCI Req | Requirement Title | Evidence Source | Control ID |
|---------|-------------------|---------------|-----------|
| 1.2 | Network segmentation | AWS Config (security groups, VPC) | PCIDSS-REQ1.2 |
| 3.4 | Render PAN unreadable | Stripe token-only architecture, AWS Config (S3 encryption) | PCIDSS-REQ3.4 |
| 5.1 | Anti-malware | CrowdStrike Falcon (future connector) | PCIDSS-REQ5.1 |
| 6.3 | Secure development | GitHub (PR enforcement, branch protection) | PCIDSS-REQ6.3 |
| 6.4.3 | Payment page scripts | GitHub (CODEOWNERS), CSP monitoring | PCIDSS-REQ6.3 |
| 7.1 | Access restriction | Okta (RBAC, admin roles) | PCIDSS-REQ7.1 |
| 8.3.2 | Password policy | Okta (password policy enforcement) | PCIDSS-REQ8.3.2 |
| 8.3.9 | MFA for remote access | Okta (MFA enrollment rate) | PCIDSS-REQ8.3.9 |
| 10.1 | Audit trails | AWS CloudTrail, Okta session logs | PCIDSS-REQ10.1 |
| 11.3 | Penetration testing | Manual + HackerOne (future) | PCIDSS-REQ11.3 |
| 11.6.1 | Payment page tamper detection | CI/CD hash verification, GitHub | PCIDSS-REQ6.3 |
| 12.1 | Security policy | RAG pipeline over policy docs | Manual |

### Network Segmentation Approach in AWS

```
                   Internet
                      |
                 [CloudFront CDN]
                      |
              [ALB - Public Subnet]
                      |
        +-------------+-------------+
        |                           |
  [App Tier - Private]    [Payment Service - Private]
  (ServiceTitan API)      (Stripe SDK only)
        |                           |
  [Data Tier - Private]             |
  (PostgreSQL RDS)                  |
  (Token storage only)    [Stripe API - Outbound only]
        |
  [Redis - Private]
  (Session/cache)
```

**Segmentation controls:**
- VPC with public/private subnet separation
- Security groups: App tier allows inbound only from ALB; data tier allows inbound only from app tier
- Payment service subnet has outbound-only NAT gateway restricted to Stripe API IPs
- No direct internet access for data tier or payment service
- VPC Flow Logs enabled on all subnets (evidence: AWS Config connector, PCIDSS-REQ1.2)
- Network ACLs as defense-in-depth layer
- AWS Config rules: `RESTRICTED_SSH`, `VPC_FLOW_LOGS_ENABLED` monitored continuously
