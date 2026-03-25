import * as fs from 'node:fs';
import * as path from 'node:path';
import postgres from 'postgres';

/**
 * Runs the control library seed SQL against the database.
 */
async function main(): Promise<void> {
  const databaseUrl = process.env['DATABASE_URL'];
  if (!databaseUrl) {
    throw new Error('DATABASE_URL environment variable is required');
  }

  const sql = postgres(databaseUrl);

  const seedFile = path.resolve(__dirname, '../../../db/seed/control-library.sql');
  const content = fs.readFileSync(seedFile, 'utf-8');

  process.stdout.write('Seeding control library...\n');
  await sql.unsafe(content);
  process.stdout.write('Seed complete.\n');

  await sql.end();
}

main().catch((err: unknown) => {
  process.stderr.write(`Seed failed: ${err instanceof Error ? err.message : String(err)}\n`);
  process.exit(1);
});
