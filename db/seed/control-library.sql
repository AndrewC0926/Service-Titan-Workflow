-- control-library.sql
-- Seed data for the master control library.
--
-- Cross-framework mapping assumptions (test_once_ids):
--   Access control:    SOC2-CC6.1  <-> ISO27001-A.9.2.3  <-> PCIDSS-REQ7.1
--   Password policy:   SOC2-CC6.2  <-> ISO27001-A.9.2.5  <-> PCIDSS-REQ8.3.2
--   MFA enforcement:   SOC2-CC6.3  <-> ISO27001-A.9.4.2  <-> PCIDSS-REQ8.3.9
--   Network security:  SOC2-CC6.6  <-> ISO27001-A.10.1.1 <-> PCIDSS-REQ1.2
--   Malware protection:SOC2-CC6.7  <-> ISO27001-A.12.2.1 <-> PCIDSS-REQ5.1
--   Vuln management:   SOC2-CC6.8  <-> ISO27001-A.12.6.1 <-> PCIDSS-REQ11.3
--   Change management: SOC2-CC8.1  <-> ISO27001-A.14.2.2 <-> PCIDSS-REQ6.3
--   Incident response: SOC2-CC7.2  <-> ISO27001-A.16.1.1 <-> PCIDSS-REQ10.1
--   Monitoring:        SOC2-CC7.3  <-> PCIDSS-REQ10.1
--   Data protection:   PCIDSS-REQ3.4 <-> ISO27001-A.10.1.1

INSERT INTO controls (id, framework, title, description, tier, test_once_ids)
VALUES
  -- SOC2 Controls
  ('SOC2-CC6.1', 'SOC2', 'Logical and Physical Access Controls',
   'The entity implements logical access security software, infrastructure, and architectures over protected information assets.',
   1, ARRAY['ISO27001-A.9.2.3', 'PCIDSS-REQ7.1']),

  ('SOC2-CC6.2', 'SOC2', 'User Authentication and Password Policy',
   'Prior to issuing system credentials, the entity registers and authorizes new users.',
   1, ARRAY['ISO27001-A.9.2.5', 'PCIDSS-REQ8.3.2']),

  ('SOC2-CC6.3', 'SOC2', 'Multi-Factor Authentication',
   'The entity requires multi-factor authentication for system access.',
   1, ARRAY['ISO27001-A.9.4.2', 'PCIDSS-REQ8.3.9']),

  ('SOC2-CC6.6', 'SOC2', 'Network Security and Boundary Protection',
   'The entity implements controls to prevent or detect and act upon the introduction of unauthorized or malicious software.',
   2, ARRAY['ISO27001-A.10.1.1', 'PCIDSS-REQ1.2']),

  ('SOC2-CC6.7', 'SOC2', 'Malware Detection and Prevention',
   'The entity deploys anti-malware technologies to detect and prevent malicious software.',
   2, ARRAY['ISO27001-A.12.2.1', 'PCIDSS-REQ5.1']),

  ('SOC2-CC6.8', 'SOC2', 'Vulnerability Management',
   'The entity assesses and manages vulnerabilities in infrastructure and software.',
   2, ARRAY['ISO27001-A.12.6.1', 'PCIDSS-REQ11.3']),

  ('SOC2-CC7.2', 'SOC2', 'Security Incident Detection and Response',
   'The entity monitors system components and identifies anomalies indicative of security incidents.',
   1, ARRAY['ISO27001-A.16.1.1', 'PCIDSS-REQ10.1']),

  ('SOC2-CC7.3', 'SOC2', 'Security Event Monitoring',
   'The entity monitors for indicators of compromise and evaluates events to determine security incidents.',
   2, ARRAY['PCIDSS-REQ10.1']),

  ('SOC2-CC8.1', 'SOC2', 'Change Management',
   'The entity authorizes, designs, develops, configures, documents, tests, approves, and implements changes.',
   1, ARRAY['ISO27001-A.14.2.2', 'PCIDSS-REQ6.3']),

  -- ISO 27001 Controls
  ('ISO27001-A.9.2.3', 'ISO27001', 'Management of Privileged Access Rights',
   'The allocation and use of privileged access rights shall be restricted and controlled.',
   1, ARRAY['SOC2-CC6.1', 'PCIDSS-REQ7.1']),

  ('ISO27001-A.9.2.5', 'ISO27001', 'Review of User Access Rights',
   'Asset owners shall review users'' access rights at regular intervals.',
   2, ARRAY['SOC2-CC6.2', 'PCIDSS-REQ8.3.2']),

  ('ISO27001-A.9.4.2', 'ISO27001', 'Secure Log-on Procedures',
   'Where required by the access control policy, access to systems shall be controlled by a secure log-on procedure.',
   1, ARRAY['SOC2-CC6.3', 'PCIDSS-REQ8.3.9']),

  ('ISO27001-A.10.1.1', 'ISO27001', 'Policy on Use of Cryptographic Controls',
   'A policy on the use of cryptographic controls for protection of information shall be developed and implemented.',
   2, ARRAY['SOC2-CC6.6', 'PCIDSS-REQ1.2', 'PCIDSS-REQ3.4']),

  ('ISO27001-A.12.2.1', 'ISO27001', 'Controls Against Malware',
   'Detection, prevention, and recovery controls to protect against malware shall be implemented.',
   2, ARRAY['SOC2-CC6.7', 'PCIDSS-REQ5.1']),

  ('ISO27001-A.12.6.1', 'ISO27001', 'Management of Technical Vulnerabilities',
   'Information about technical vulnerabilities of information systems shall be obtained and evaluated.',
   2, ARRAY['SOC2-CC6.8', 'PCIDSS-REQ11.3']),

  ('ISO27001-A.14.2.2', 'ISO27001', 'System Change Control Procedures',
   'Changes to systems within the development lifecycle shall be controlled by formal change control procedures.',
   1, ARRAY['SOC2-CC8.1', 'PCIDSS-REQ6.3']),

  ('ISO27001-A.16.1.1', 'ISO27001', 'Responsibilities and Procedures for Incident Management',
   'Management responsibilities and procedures shall be established for security incident response.',
   1, ARRAY['SOC2-CC7.2', 'PCIDSS-REQ10.1']),

  -- PCI DSS Controls
  ('PCIDSS-REQ1.2', 'PCIDSS', 'Restrict Connections Between Untrusted Networks',
   'Restrict inbound and outbound traffic to that which is necessary for the cardholder data environment.',
   1, ARRAY['SOC2-CC6.6', 'ISO27001-A.10.1.1']),

  ('PCIDSS-REQ3.4', 'PCIDSS', 'Render PAN Unreadable',
   'Render PAN unreadable anywhere it is stored using strong cryptography.',
   1, ARRAY['ISO27001-A.10.1.1']),

  ('PCIDSS-REQ5.1', 'PCIDSS', 'Deploy Anti-Virus on Susceptible Systems',
   'Deploy anti-virus software on all systems commonly affected by malicious software.',
   2, ARRAY['SOC2-CC6.7', 'ISO27001-A.12.2.1']),

  ('PCIDSS-REQ6.3', 'PCIDSS', 'Develop Secure Software Applications',
   'Develop internal and external software applications securely following industry standards.',
   1, ARRAY['SOC2-CC8.1', 'ISO27001-A.14.2.2']),

  ('PCIDSS-REQ7.1', 'PCIDSS', 'Limit Access to System Components',
   'Limit access to system components and cardholder data to only those individuals whose job requires such access.',
   1, ARRAY['SOC2-CC6.1', 'ISO27001-A.9.2.3']),

  ('PCIDSS-REQ8.3.2', 'PCIDSS', 'Strong Password Requirements',
   'Require minimum password complexity for user accounts.',
   2, ARRAY['SOC2-CC6.2', 'ISO27001-A.9.2.5']),

  ('PCIDSS-REQ8.3.9', 'PCIDSS', 'Multi-Factor Authentication for Remote Access',
   'MFA is implemented for all remote network access originating from outside the entity''s network.',
   1, ARRAY['SOC2-CC6.3', 'ISO27001-A.9.4.2']),

  ('PCIDSS-REQ10.1', 'PCIDSS', 'Audit Trails for System Components',
   'Implement audit trails to link all access to system components to each individual user.',
   1, ARRAY['SOC2-CC7.2', 'SOC2-CC7.3', 'ISO27001-A.16.1.1']),

  ('PCIDSS-REQ11.3', 'PCIDSS', 'Penetration Testing',
   'Perform external and internal penetration testing regularly and after significant changes.',
   2, ARRAY['SOC2-CC6.8', 'ISO27001-A.12.6.1']),

  -- ISO 42001 Controls
  ('ISO42001-6.1.2', 'ISO42001', 'AI Risk Assessment',
   'The organization shall identify and assess risks related to the development, deployment, and use of AI systems.',
   1, ARRAY[]::TEXT[]),

  ('ISO42001-8.4.1', 'ISO42001', 'AI System Documentation',
   'The organization shall document AI system design, data requirements, and operational parameters.',
   2, ARRAY[]::TEXT[]),

  ('ISO42001-9.1.1', 'ISO42001', 'AI Performance Monitoring',
   'The organization shall monitor, measure, and evaluate AI system performance against defined objectives.',
   2, ARRAY[]::TEXT[]),

  ('ISO42001-10.1.1', 'ISO42001', 'AI Continual Improvement',
   'The organization shall continually improve the suitability, adequacy, and effectiveness of AI management.',
   3, ARRAY[]::TEXT[])

ON CONFLICT (id) DO UPDATE SET
  title = EXCLUDED.title,
  description = EXCLUDED.description,
  tier = EXCLUDED.tier,
  test_once_ids = EXCLUDED.test_once_ids;
