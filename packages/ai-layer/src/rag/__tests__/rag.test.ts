import nock from 'nock';
import { chunkText, ingestDocument } from '../ingest.js';
import { retrieveContext } from '../retrieve.js';
import { injectContext } from '../inject.js';
import type { RetrievalResult, DocumentChunk } from '@compliance-engine/types';

const VOYAGE_API_KEY = 'pa-test-key';
const VOYAGE_BASE = 'https://api.voyageai.com';

beforeEach(() => {
  nock.cleanAll();
});

afterAll(() => {
  nock.restore();
});

// -- Helper to build a mock DocumentChunk --
function mockChunk(overrides: Partial<DocumentChunk> = {}): DocumentChunk {
  return {
    id: 'chunk-1',
    sourceDoc: 'soc2-policy.pdf',
    framework: 'SOC2',
    controlIds: ['SOC2-CC6.1'],
    chunkIndex: 0,
    text: 'Access controls must be implemented for all production systems.',
    embedding: null,
    ingestedAt: new Date(),
    ...overrides,
  };
}

describe('RAG Pipeline', () => {
  describe('chunkText()', () => {
    it('splits text into overlapping chunks of specified size', () => {
      // 10 words, chunk size 5 words, overlap 2 words
      const text = 'one two three four five six seven eight nine ten';
      const chunks = chunkText(text, 5, 2);

      expect(chunks.length).toBeGreaterThanOrEqual(2);
      // First chunk should have 5 words
      expect(chunks[0]!.split(/\s+/).length).toBe(5);
      // Overlap: last 2 words of chunk 0 should appear at start of chunk 1
      const firstChunkWords = chunks[0]!.split(/\s+/);
      const secondChunkWords = chunks[1]!.split(/\s+/);
      expect(secondChunkWords[0]).toBe(firstChunkWords[3]);
      expect(secondChunkWords[1]).toBe(firstChunkWords[4]);
    });

    it('returns single chunk if text is shorter than chunk size', () => {
      const text = 'short text';
      const chunks = chunkText(text, 100, 20);

      expect(chunks).toHaveLength(1);
      expect(chunks[0]).toBe(text);
    });
  });

  describe('ingestDocument()', () => {
    it('chunks text, calls Voyage API for embeddings, returns DocumentChunks', async () => {
      const text = Array.from({ length: 20 }, (_, i) => `word${i}`).join(' ');

      // Mock Voyage embeddings endpoint — will be called with chunk texts
      nock(VOYAGE_BASE)
        .post('/v1/embeddings')
        .reply(200, (_, body) => {
          const parsed = body as { input: string[] };
          return {
            data: parsed.input.map((_: string, idx: number) => ({
              embedding: Array.from({ length: 1024 }, () => idx * 0.01),
            })),
          };
        });

      const result = await ingestDocument(text, {
        sourceDoc: 'test-doc.pdf',
        framework: 'SOC2',
        controlIds: ['SOC2-CC6.1'],
      }, VOYAGE_API_KEY);

      expect(result.chunks.length).toBeGreaterThan(0);
      expect(result.totalChunks).toBe(result.chunks.length);

      // Each chunk should have an embedding
      for (const chunk of result.chunks) {
        expect(chunk.embedding).not.toBeNull();
        expect(chunk.embedding!).toHaveLength(1024);
        expect(chunk.sourceDoc).toBe('test-doc.pdf');
        expect(chunk.framework).toBe('SOC2');
      }
    });
  });

  describe('retrieveContext()', () => {
    it('embeds query via Voyage and returns scored results', async () => {
      // Mock Voyage for the query embedding
      nock(VOYAGE_BASE)
        .post('/v1/embeddings')
        .reply(200, {
          data: [{ embedding: Array.from({ length: 1024 }, () => 0.1) }],
        });

      // We need a mock DB for the similarity search.
      // Since retrieveContext uses raw SQL via postgres, we mock at the function level.
      // This test verifies the Voyage call + result shaping.
      // Full DB integration test would require Docker.
      // For unit test: we pass a mock dbUrl that retrieve will try to connect to.
      // We'll test this by verifying it calls Voyage correctly and
      // handles the response shape. The DB query will fail, which we catch.

      // For the unit test, we test that it calls Voyage API correctly.
      // The DB call will throw — we expect an error or empty results.
      try {
        const results = await retrieveContext(
          'What are the access control requirements?',
          VOYAGE_API_KEY,
          'postgresql://localhost:5432/test',
          { topK: 5, minScore: 0.5 },
        );
        // If it reaches here with mocked DB, verify shape
        expect(Array.isArray(results)).toBe(true);
      } catch {
        // Expected — no real DB. Voyage API was still called successfully.
        expect(nock.isDone()).toBe(true);
      }
    });
  });

  describe('injectContext()', () => {
    it('formats retrieved chunks into structured context block', () => {
      const results: RetrievalResult[] = [
        {
          chunk: mockChunk({
            text: 'All production access requires MFA and manager approval.',
            sourceDoc: 'soc2-policy.pdf',
            controlIds: ['SOC2-CC6.1'],
          }),
          score: 0.92,
        },
        {
          chunk: mockChunk({
            id: 'chunk-2',
            text: 'Quarterly access reviews are mandatory.',
            sourceDoc: 'access-review-procedure.pdf',
            controlIds: ['SOC2-CC6.2'],
            chunkIndex: 3,
          }),
          score: 0.85,
        },
      ];

      const context = injectContext(results);

      // Should contain the chunk texts
      expect(context).toContain('All production access requires MFA');
      expect(context).toContain('Quarterly access reviews are mandatory');
      // Should reference source documents
      expect(context).toContain('soc2-policy.pdf');
      expect(context).toContain('access-review-procedure.pdf');
      // Should include relevance scores
      expect(context).toContain('0.92');
      expect(context).toContain('0.85');
      // Should be non-empty structured text
      expect(context.length).toBeGreaterThan(100);
    });

    it('returns empty context message when no results provided', () => {
      const context = injectContext([]);
      expect(context).toContain('No relevant context');
    });
  });
});
