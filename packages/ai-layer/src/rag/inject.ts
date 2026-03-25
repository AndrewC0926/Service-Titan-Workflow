import type { RetrievalResult } from '@compliance-engine/types';

/**
 * Format retrieved chunks into a structured context block for insertion
 * into the Claude prompt's user turn.
 *
 * Each chunk is presented with its source document, relevance score,
 * and associated control IDs so the drafter can cite sources.
 */
export function injectContext(results: RetrievalResult[]): string {
  if (results.length === 0) {
    return '[No relevant context documents found. Flag any claims as EVIDENCE_MISSING.]';
  }

  const header = `--- CONTEXT DOCUMENTS (${results.length} sources) ---\n\n`;

  const blocks = results.map((result, idx) => {
    const { chunk, score } = result;
    return [
      `[Source ${idx + 1}]`,
      `Document: ${chunk.sourceDoc}`,
      `Relevance: ${score.toFixed(2)}`,
      `Controls: ${chunk.controlIds.join(', ') || 'none'}`,
      `Framework: ${chunk.framework ?? 'unspecified'}`,
      '',
      chunk.text,
      '',
    ].join('\n');
  });

  const footer = '--- END CONTEXT ---';

  return header + blocks.join('\n') + footer;
}
