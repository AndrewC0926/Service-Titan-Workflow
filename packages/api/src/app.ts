import express from 'express';
import { requestLogger } from './middleware/request-logger.js';
import { errorHandler } from './middleware/error-handler.js';
import { apiRateLimiter } from './middleware/rate-limit.js';
import { trustRoutes } from './routes/trust.routes.js';
import { dashboardRoutes } from './routes/dashboard.routes.js';

export function createApp(): express.Express {
  const app = express();

  // Middleware
  app.use(express.json());
  app.use(requestLogger);
  app.use('/api', apiRateLimiter);

  // Health check (no rate limit)
  app.get('/health', (_req, res) => {
    res.json({ status: 'ok', timestamp: new Date().toISOString() });
  });

  // Routes
  app.use('/api/trust', trustRoutes);
  app.use('/api/dashboard', dashboardRoutes);

  // 404 handler
  app.use((_req, res) => {
    res.status(404).json({ error: 'Not found' });
  });

  // Error handler
  app.use(errorHandler);

  return app;
}
