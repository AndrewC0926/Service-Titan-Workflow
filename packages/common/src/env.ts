import { z } from 'zod';

/**
 * Base environment schema with variables required by all packages.
 * Individual packages extend this with their own required vars.
 */
export const baseEnvSchema = z.object({
  DATABASE_URL: z.string().url(),
  REDIS_URL: z.string().url(),
  NODE_ENV: z.enum(['development', 'production', 'test']).default('development'),
  LOG_LEVEL: z.enum(['debug', 'info', 'warn', 'error']).default('info'),
  PORT: z.coerce.number().int().positive().default(3000),
});

export type BaseEnv = z.infer<typeof baseEnvSchema>;

/**
 * Validate environment variables at startup. Throws immediately if
 * any required variable is missing or invalid — fail fast, not at runtime.
 */
export function validateEnv<T extends z.ZodTypeAny>(schema: T): z.infer<T> {
  const result = schema.safeParse(process.env);
  if (!result.success) {
    const formatted = result.error.issues
      .map((issue) => `  ${issue.path.join('.')}: ${issue.message}`)
      .join('\n');
    throw new Error(`Environment validation failed:\n${formatted}`);
  }
  return result.data as z.infer<T>;
}
