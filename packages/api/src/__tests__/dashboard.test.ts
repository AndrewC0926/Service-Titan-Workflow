import request from 'supertest';
import { createApp } from '../app.js';

const app = createApp();

describe('Dashboard API', () => {
  describe('GET /api/dashboard/posture', () => {
    it('returns overall compliance score', async () => {
      const res = await request(app).get('/api/dashboard/posture');
      expect(res.status).toBe(200);
      expect(res.body).toHaveProperty('overallScore');
      expect(typeof res.body.overallScore).toBe('number');
      expect(res.body).toHaveProperty('frameworkScores');
    });
  });

  describe('GET /api/dashboard/controls', () => {
    it('returns all controls with current status', async () => {
      const res = await request(app).get('/api/dashboard/controls');
      expect(res.status).toBe(200);
      expect(res.body).toHaveProperty('controls');
      expect(Array.isArray(res.body.controls)).toBe(true);
    });
  });

  describe('GET /api/dashboard/drift', () => {
    it('returns recent drift events', async () => {
      const res = await request(app).get('/api/dashboard/drift');
      expect(res.status).toBe(200);
      expect(res.body).toHaveProperty('events');
      expect(Array.isArray(res.body.events)).toBe(true);
      expect(res.body).toHaveProperty('period');
    });
  });

  describe('GET /api/dashboard/gaps', () => {
    it('returns controls with fail/stale status grouped by framework', async () => {
      const res = await request(app).get('/api/dashboard/gaps');
      expect(res.status).toBe(200);
      expect(res.body).toHaveProperty('gaps');
      expect(res.body).toHaveProperty('totalGaps');
      expect(typeof res.body.totalGaps).toBe('number');
    });
  });

  describe('GET /api/dashboard/scorecard', () => {
    it('returns board-level summary', async () => {
      const res = await request(app).get('/api/dashboard/scorecard');
      expect(res.status).toBe(200);
      expect(res.body).toHaveProperty('overallScore');
      expect(res.body).toHaveProperty('criticalGaps');
      expect(res.body).toHaveProperty('mttrHours');
      expect(res.body).toHaveProperty('generatedAt');
    });
  });
});
