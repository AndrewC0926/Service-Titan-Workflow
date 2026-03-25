import * as https from 'node:https';
import type { DocumentChunk, Framework } from '@compliance-engine/types';
import { logger } from '@compliance-engine/common';

const VOYAGE_HOST = 'api.voyageai.com';
const VOYAGE_MODEL = 'voyage-3';
const EMBEDDING_DIM = 1024;

export interface IngestOptions {
  sourceDoc: string;
  framework: Framework | null;
  controlIds: string[];
}

export interface ChunkResult {
  chunks: DocumentChunk[];
  totalChunks: number;
}

/**
 * Split text into overlapping chunks (word-based).
 * @param text - Full document text
 * @param chunkSize - Number of words per chunk
 * @param overlap - Number of overlapping words between chunks
 */
export function chunkText(text: string, chunkSize: number, overlap: number): string[] {
  const words = text.split(/\s+/).filter((w) => w.length > 0);

  if (words.length <= chunkSize) {
    return [words.join(' ')];
  }

  const chunks: string[] = [];
  const step = chunkSize - overlap;
  let start = 0;

  while (start < words.length) {
    const end = Math.min(start + chunkSize, words.length);
    chunks.push(words.slice(start, end).join(' '));
    if (end >= words.length) break;
    start += step;
  }

  return chunks;
}

/**
 * Chunk a document, embed each chunk via Voyage AI, return DocumentChunks.
 */
export async function ingestDocument(
  text: string,
  options: IngestOptions,
  voyageApiKey: string,
): Promise<ChunkResult> {
  const textChunks = chunkText(text, 500, 100);

  logger.info('Ingesting document', {
    sourceDoc: options.sourceDoc,
    chunkCount: textChunks.length,
  });

  const embeddings = await fetchEmbeddings(textChunks, voyageApiKey);

  const chunks: DocumentChunk[] = textChunks.map((chunk, idx) => ({
    id: crypto.randomUUID(),
    sourceDoc: options.sourceDoc,
    framework: options.framework,
    controlIds: options.controlIds,
    chunkIndex: idx,
    text: chunk,
    embedding: embeddings[idx] ?? null,
    ingestedAt: new Date(),
  }));

  return { chunks, totalChunks: chunks.length };
}

/**
 * Call Voyage AI embeddings API. Uses node:https for nock compatibility in tests.
 */
async function fetchEmbeddings(texts: string[], apiKey: string): Promise<number[][]> {
  const body = JSON.stringify({
    input: texts,
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

  return parsed.data.map((d) => {
    if (d.embedding.length !== EMBEDDING_DIM) {
      logger.warn('Unexpected embedding dimension', {
        expected: EMBEDDING_DIM,
        actual: d.embedding.length,
      });
    }
    return d.embedding;
  });
}
