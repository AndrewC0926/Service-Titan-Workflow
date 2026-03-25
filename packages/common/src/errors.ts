/**
 * Base error class for all compliance engine errors.
 * Provides a structured `code` field for programmatic handling.
 */
export class AppError extends Error {
  constructor(
    message: string,
    public readonly code: string,
    public readonly statusCode: number = 500,
  ) {
    super(message);
    this.name = 'AppError';
  }
}

export class AuthError extends AppError {
  constructor(message: string) {
    super(message, 'AUTH_ERROR', 401);
    this.name = 'AuthError';
  }
}

export class RateLimitError extends AppError {
  constructor(
    message: string,
    public readonly retryAfterMs: number,
  ) {
    super(message, 'RATE_LIMIT', 429);
    this.name = 'RateLimitError';
  }
}

export class ConnectorError extends AppError {
  constructor(message: string, public readonly source: string) {
    super(message, 'CONNECTOR_ERROR', 502);
    this.name = 'ConnectorError';
  }
}
