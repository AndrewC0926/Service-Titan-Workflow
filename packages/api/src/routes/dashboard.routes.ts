import { Router, type Router as RouterType } from 'express';
import type { Framework, ControlStatus } from '@compliance-engine/types';

const router: RouterType = Router();

// Stub data — in production these queries run against the DB via Drizzle.
// Stub design matches the exact schema so the switch to real queries is mechanical.

interface ControlSummary {
  id: string;
  framework: Framework;
  title: string;
  status: ControlStatus;
  confidence: number;
  lastEvidenceAt: string | null;
  tier: number;
}

const STUB_CONTROLS: ControlSummary[] = [
  { id: 'SOC2-CC6.1', framework: 'SOC2', title: 'Logical and Physical Access Controls', status: 'pass', confidence: 0.95, lastEvidenceAt: '2026-03-25T10:00:00Z', tier: 1 },
  { id: 'SOC2-CC8.1', framework: 'SOC2', title: 'Change Management', status: 'pass', confidence: 1.0, lastEvidenceAt: '2026-03-25T08:00:00Z', tier: 1 },
  { id: 'PCIDSS-REQ1.2', framework: 'PCIDSS', title: 'Restrict Connections', status: 'pass', confidence: 1.0, lastEvidenceAt: '2026-03-25T09:30:00Z', tier: 1 },
  { id: 'PCIDSS-REQ3.4', framework: 'PCIDSS', title: 'Render PAN Unreadable', status: 'fail', confidence: 0.0, lastEvidenceAt: null, tier: 1 },
  { id: 'ISO27001-A.9.2.3', framework: 'ISO27001', title: 'Privileged Access Rights', status: 'drift', confidence: 0.5, lastEvidenceAt: '2026-03-25T06:00:00Z', tier: 1 },
  { id: 'ISO42001-6.1.2', framework: 'ISO42001', title: 'AI Risk Assessment', status: 'pending', confidence: 0.0, lastEvidenceAt: null, tier: 1 },
];

/** GET /api/dashboard/posture — overall compliance score */
router.get('/posture', (_req, res) => {
  const scored = STUB_CONTROLS.filter((c) => c.confidence > 0);
  const overallScore = scored.length > 0
    ? Math.round((scored.reduce((sum, c) => sum + c.confidence, 0) / scored.length) * 100) / 100
    : 0;

  const frameworkScores = (['SOC2', 'ISO27001', 'PCIDSS', 'ISO42001'] as Framework[]).map((fw) => {
    const fwControls = scored.filter((c) => c.framework === fw);
    const score = fwControls.length > 0
      ? Math.round((fwControls.reduce((sum, c) => sum + c.confidence, 0) / fwControls.length) * 100) / 100
      : 0;
    return { framework: fw, score, controlCount: STUB_CONTROLS.filter((c) => c.framework === fw).length };
  });

  res.json({ overallScore, frameworkScores });
});

/** GET /api/dashboard/controls — all controls with status */
router.get('/controls', (_req, res) => {
  res.json({ controls: STUB_CONTROLS, total: STUB_CONTROLS.length });
});

/** GET /api/dashboard/drift — recent drift events (last 30 days) */
router.get('/drift', (_req, res) => {
  // Stub drift events
  const events = [
    {
      id: 'drift-1',
      controlId: 'ISO27001-A.9.2.3',
      prevStatus: 'pass',
      newStatus: 'drift',
      detectedAt: '2026-03-24T14:00:00Z',
      resolvedAt: null,
      jiraTicketId: 'COMP-101',
    },
  ];

  res.json({ events, period: '30d', total: events.length });
});

/** GET /api/dashboard/gaps — controls with fail/stale grouped by framework */
router.get('/gaps', (_req, res) => {
  const gapControls = STUB_CONTROLS.filter(
    (c) => c.status === 'fail' || c.status === 'stale',
  );

  const byFramework: Record<string, ControlSummary[]> = {};
  for (const control of gapControls) {
    const fw = control.framework;
    if (!byFramework[fw]) byFramework[fw] = [];
    byFramework[fw].push(control);
  }

  res.json({ gaps: byFramework, totalGaps: gapControls.length });
});

/** GET /api/dashboard/scorecard — board-level summary */
router.get('/scorecard', (_req, res) => {
  const scored = STUB_CONTROLS.filter((c) => c.confidence > 0);
  const overallScore = scored.length > 0
    ? Math.round((scored.reduce((sum, c) => sum + c.confidence, 0) / scored.length) * 100) / 100
    : 0;

  const criticalGaps = STUB_CONTROLS.filter(
    (c) => c.tier === 1 && (c.status === 'fail' || c.status === 'stale'),
  ).length;

  res.json({
    overallScore,
    criticalGaps,
    controlsMonitored: STUB_CONTROLS.length,
    frameworksCovered: ['SOC2', 'ISO27001', 'PCIDSS', 'ISO42001'],
    mttrHours: 4.2, // stub — calculated from drift event resolved_at - detected_at
    generatedAt: new Date().toISOString(),
  });
});

export { router as dashboardRoutes };
