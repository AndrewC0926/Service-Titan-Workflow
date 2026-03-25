import { Router, type Router as RouterType } from 'express';
import { z } from 'zod';
import type { Framework } from '@compliance-engine/types';
import { logger } from '@compliance-engine/common';

const router: RouterType = Router();

// In-memory job store (replaced by DB in production)
const jobs = new Map<string, {
  jobId: string;
  status: 'pending' | 'processing' | 'complete';
  questions: Array<{ id: string; text: string; category: string | null }>;
  sourceDoc: string;
  createdAt: string;
}>();

// -- Schemas --

const questionSchema = z.object({
  id: z.string(),
  text: z.string().min(1),
  category: z.string().nullable().optional().default(null),
});

const questionnaireSubmitSchema = z.object({
  questions: z.array(questionSchema).min(1),
  sourceDoc: z.string().min(1),
});

const ndaSchema = z.object({
  signerName: z.string().min(1),
  signerEmail: z.string().email(),
  companyName: z.string().min(1),
});

// -- Routes --

/** GET /api/trust/status — live cert status per framework */
router.get('/status', (_req, res) => {
  const frameworks: Array<{
    framework: Framework;
    certStatus: string;
    lastAudit: string | null;
    nextAudit: string | null;
  }> = [
    { framework: 'SOC2', certStatus: 'active', lastAudit: '2025-12-15', nextAudit: '2026-12-15' },
    { framework: 'ISO27001', certStatus: 'active', lastAudit: '2025-10-01', nextAudit: '2026-10-01' },
    { framework: 'PCIDSS', certStatus: 'active', lastAudit: '2026-01-20', nextAudit: '2027-01-20' },
    { framework: 'ISO42001', certStatus: 'in_progress', lastAudit: null, nextAudit: '2026-06-01' },
  ];

  res.json({ frameworks });
});

/** GET /api/trust/documents/:type — NDA-gated doc delivery (stub) */
router.get('/documents/:type', (req, res) => {
  const docType = req.params['type'];
  // In production: verify NDA on file, return presigned S3 URL
  res.json({
    documentType: docType,
    url: `https://s3.amazonaws.com/compliance-docs/${docType}.pdf`,
    expiresAt: new Date(Date.now() + 3600 * 1000).toISOString(),
    note: 'Stub URL — NDA verification not yet implemented',
  });
});

/** POST /api/trust/questionnaire — submit questions for auto-drafting */
router.post('/questionnaire', (req, res) => {
  const parsed = questionnaireSubmitSchema.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({
      error: 'Invalid request body',
      details: parsed.error.issues.map((i) => `${i.path.join('.')}: ${i.message}`),
    });
    return;
  }

  const jobId = crypto.randomUUID();
  jobs.set(jobId, {
    jobId,
    status: 'pending',
    questions: parsed.data.questions.map((q) => ({
      id: q.id,
      text: q.text,
      category: q.category ?? null,
    })),
    sourceDoc: parsed.data.sourceDoc,
    createdAt: new Date().toISOString(),
  });

  logger.info('Questionnaire job created', { jobId, questionCount: parsed.data.questions.length });
  res.status(202).json({ jobId, status: 'pending' });
});

/** GET /api/trust/questionnaire/:jobId — poll for draft results */
router.get('/questionnaire/:jobId', (req, res) => {
  const jobId = req.params['jobId']!;
  const job = jobs.get(jobId);

  if (!job) {
    res.status(404).json({ error: `Job ${jobId} not found` });
    return;
  }

  res.json({
    jobId: job.jobId,
    status: job.status,
    questionCount: job.questions.length,
    createdAt: job.createdAt,
  });
});

/** POST /api/trust/nda — record NDA signature */
router.post('/nda', (req, res) => {
  const parsed = ndaSchema.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({
      error: 'Invalid request body',
      details: parsed.error.issues.map((i) => `${i.path.join('.')}: ${i.message}`),
    });
    return;
  }

  const ndaId = crypto.randomUUID();
  const signedAt = new Date().toISOString();

  logger.info('NDA recorded', {
    ndaId,
    signerEmail: parsed.data.signerEmail,
    companyName: parsed.data.companyName,
  });

  res.status(201).json({
    ndaId,
    signerName: parsed.data.signerName,
    signerEmail: parsed.data.signerEmail,
    companyName: parsed.data.companyName,
    signedAt,
  });
});

export { router as trustRoutes };
