import { drizzle } from 'drizzle-orm/postgres-js';
import postgres from 'postgres';
import * as schema from './schema.js';

/**
 * Create a Drizzle database client connected to the given DATABASE_URL.
 * Caller is responsible for validating the env var before calling this.
 */
export function createDb(databaseUrl: string) {
  const client = postgres(databaseUrl);
  return drizzle(client, { schema });
}

export type Db = ReturnType<typeof createDb>;
