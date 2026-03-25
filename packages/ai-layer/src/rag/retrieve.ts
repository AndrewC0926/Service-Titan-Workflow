import * as https from 'node:https';
import type { RetrievalResult, DocumentChunk } from '@compliance-engine/types';
import { logger } from '@compliance-engine/common';
import postgres from 'postgres';

const VOYAGE_HOST = 'api.voyageai.com';
const VOYAGE_MODEL = 'voyage-3';

export interface RetrieveOptions {
  topK: number;
  minScore: number;
  framework?: string;
}

const DEFAULT_OPTIONS: RetrieveOptions = {
  topK: 5,
  minScore: 0.5,
};

/**
 * Embed the query via Voyage AI, then run cosine similarity search
 * against pgvector document_chunks table. Returns top-k results above minScore.
 */
export async function retrieveContext(
  query: string,
  voyageApiKey: string,
  dbUrl: string,
  options?: Partial<RetrieveOptions>,
): Promise<RetrievalResult[]> {
  const opts = { ...DEFAULT_OPTIONS, ...options };

  // Embed the query
  const queryEmbedding = await embedQuery(query, voyageApiKey);
  const embeddingStr = `[${queryEmbedding.join(',')}]`;

  // Cosine similarity search via raw SQL (pgvector)
  const sql = postgres(dbUrl);

  try {
    const frameworkFilter = opts.framework
      ? sql`AND framework = ${opts.framework}`
      : sql``;

    const rows = await sql<Array<{
      id: string;
      source_doc: string;
      framework: string | null;
      control_ids: string[];
      chunk_index: number;
      text: string;
      score: number;
    }>>`
      SELECT
        id,
        source_doc,
        framework,
        control_ids,
        chunk_index,
        text,
        1 - (embedding <=> ${embeddingStr}::vector) AS score
      FROM document_chunks
      WHERE 1 - (embedding <=> ${embeddingStr}::vector) >= ${opts.minScore}
        ${frameworkFilter}
      ORDER BY embedding <=> ${embeddingStr}::vector
      LIMIT ${opts.topK}
    `;

    return rows.map((row) => ({
      chunk: {
        id: row.id,
        sourceDoc: row.source_doc,
        framework: row.framework as DocumentChunk['framework'],
        controlIds: row.control_ids,
        chunkIndex: row.chunk_index,
        text: row.text,
        embedding: null, // Don't return embeddings in search results
        ingestedAt: new Date(),
      },
      score: Number(row.score),
    }));
  } finally {
    await sql.end();
  }
}

/**
 * Embed a single query string via Voyage AI.
 */
async function embedQuery(query: string, apiKey: string): Promise<number[]> {
  const body = JSON.stringify({
    input: [query],
    model: VOYAGE_MODEL,
  });

  const response = await new Promise<string>((resolve, reject) => {
    const req = https.request(
      {
        hostname: VOYAGE_HOST,
        path: '/v1/embeddings',
        method: 'POST',
        headers: {
          Authorization: `Bearer ${apiKey}`,
          'Content-Type': 'application/json',
          'User-Agent': 'compliance-engine',
        },
      },
      (res) => {
        const chunks: Buffer[] = [];
        res.on('data', (chunk: Buffer) => chunks.push(chunk));
        res.on('end', () => resolve(Buffer.concat(chunks).toString('utf-8')));
      },
    );

    req.on('error', reject);
    req.write(body);
    req.end();
  });

  const parsed = JSON.parse(response) as {
    data: Array<{ embedding: number[] }>;
  };

  const embedding = parsed.data[0]?.embedding;
  if (!embedding) {
    throw new Error('Voyage API returned no embedding');
  }

  logger.debug('Query embedded', { dimension: embedding.length });
  return embedding;
}
