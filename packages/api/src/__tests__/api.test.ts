import request from 'supertest';
import { createApp } from '../app.js';

const app = createApp();

describe('REST API', () => {
  describe('GET /health', () => {
    it('returns 200 with status ok', async () => {
      const res = await request(app).get('/health');
      expect(res.status).toBe(200);
      expect(res.body).toMatchObject({ status: 'ok' });
    });
  });

  describe('GET /api/trust/status', () => {
    it('returns cert status per framework', async () => {
      const res = await request(app).get('/api/trust/status');
      expect(res.status).toBe(200);
      expect(res.body).toHaveProperty('frameworks');
      expect(Array.isArray(res.body.frameworks)).toBe(true);
    });
  });

  describe('POST /api/trust/questionnaire', () => {
    it('accepts questions and returns a jobId', async () => {
      const res = await request(app)
        .post('/api/trust/questionnaire')
        .send({
          questions: [
            { id: 'Q1', text: 'Do you enforce MFA?', category: 'Access Control' },
          ],
          sourceDoc: 'vendor-questionnaire.txt',
        });
      expect(res.status).toBe(202);
      expect(res.body).toHaveProperty('jobId');
      expect(typeof res.body.jobId).toBe('string');
    });

    it('returns 400 for invalid request body', async () => {
      const res = await request(app)
        .post('/api/trust/questionnaire')
        .send({ invalid: true });
      expect(res.status).toBe(400);
      expect(res.body).toHaveProperty('error');
    });
  });

  describe('GET /api/trust/questionnaire/:jobId', () => {
    it('returns job status for valid jobId', async () => {
      // First create a job
      const createRes = await request(app)
        .post('/api/trust/questionnaire')
        .send({
          questions: [
            { id: 'Q1', text: 'Do you enforce MFA?', category: 'Access Control' },
          ],
          sourceDoc: 'test.txt',
        });
      const jobId = createRes.body.jobId as string;

      const res = await request(app).get(`/api/trust/questionnaire/${jobId}`);
      expect(res.status).toBe(200);
      expect(res.body).toHaveProperty('jobId', jobId);
      expect(res.body).toHaveProperty('status');
    });
  });

  describe('POST /api/trust/nda', () => {
    it('records NDA signature and returns confirmation', async () => {
      const res = await request(app)
        .post('/api/trust/nda')
        .send({
          signerName: 'Jane Doe',
          signerEmail: 'jane@example.com',
          companyName: 'Acme Corp',
        });
      expect(res.status).toBe(201);
      expect(res.body).toHaveProperty('ndaId');
      expect(res.body).toHaveProperty('signedAt');
    });
  });

  describe('error handling', () => {
    it('returns 404 for unknown routes', async () => {
      const res = await request(app).get('/api/nonexistent');
      expect(res.status).toBe(404);
      expect(res.body).toHaveProperty('error');
    });
  });
});
