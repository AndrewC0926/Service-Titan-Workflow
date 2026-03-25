import * as fs from 'node:fs';
import * as path from 'node:path';
import postgres from 'postgres';

/**
 * Simple migration runner that applies SQL files from db/migrations/ in order.
 * Tracks applied migrations in a _migrations table.
 */
async function main(): Promise<void> {
  const databaseUrl = process.env['DATABASE_URL'];
  if (!databaseUrl) {
    throw new Error('DATABASE_URL environment variable is required');
  }

  const sql = postgres(databaseUrl);

  // Ensure migrations tracking table exists
  await sql`
    CREATE TABLE IF NOT EXISTS _migrations (
      name TEXT PRIMARY KEY,
      applied_at TIMESTAMPTZ DEFAULT now()
    )
  `;

  // Find migration files
  const migrationsDir = path.resolve(__dirname, '../../../db/migrations');
  const files = fs.readdirSync(migrationsDir)
    .filter((f) => f.endsWith('.sql'))
    .sort();

  // Get already-applied migrations
  const applied = await sql<{ name: string }[]>`SELECT name FROM _migrations`;
  const appliedSet = new Set(applied.map((r) => r.name));

  for (const file of files) {
    if (appliedSet.has(file)) {
      process.stdout.write(`  skip: ${file} (already applied)\n`);
      continue;
    }

    const filePath = path.join(migrationsDir, file);
    const content = fs.readFileSync(filePath, 'utf-8');

    process.stdout.write(`  apply: ${file}\n`);
    await sql.unsafe(content);
    await sql`INSERT INTO _migrations (name) VALUES (${file})`;
  }

  process.stdout.write('Migrations complete.\n');
  await sql.end();
}

main().catch((err: unknown) => {
  process.stderr.write(`Migration failed: ${err instanceof Error ? err.message : String(err)}\n`);
  process.exit(1);
});
