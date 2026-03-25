import type { Request, Response, NextFunction } from 'express';
import { AppError, logger } from '@compliance-engine/common';

export function errorHandler(
  err: unknown,
  _req: Request,
  res: Response,
  _next: NextFunction,
): void {
  if (err instanceof AppError) {
    res.status(err.statusCode).json({
      error: err.message,
      code: err.code,
    });
    return;
  }

  const message = err instanceof Error ? err.message : 'Internal server error';
  logger.error('Unhandled error', { error: message });
  res.status(500).json({ error: message });
}
