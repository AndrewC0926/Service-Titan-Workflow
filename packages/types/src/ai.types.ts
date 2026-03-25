import type { Framework } from './evidence.types.js';

/** A chunked segment of a compliance document stored with its embedding. */
export interface DocumentChunk {
  id: string;
  sourceDoc: string;
  framework: Framework | null;
  controlIds: string[];
  chunkIndex: number;
  text: string;
  embedding: number[] | null; // vector(1024) — voyage-3
  ingestedAt: Date;
}

/** A chunk returned from similarity search with its relevance score. */
export interface RetrievalResult {
  chunk: DocumentChunk;
  score: number; // cosine similarity 0.0–1.0
}

/** A single question extracted from a questionnaire document. */
export interface QuestionnaireQuestion {
  id: string;
  text: string;
  category: string | null;
  framework: Framework | null;
  sourceDoc: string;
}

/** Review tier assignment for a drafted response. */
export type ReviewTier = 'auto_approve' | 'sme_review' | 'legal_review' | 'manual';

/** An AI-drafted response to a questionnaire question. */
export interface DraftedResponse {
  questionId: string;
  questionText: string;
  draftResponse: string;
  confidence: number; // 0.0–1.0
  sources: string[];  // source doc references used
  flags: string[];    // e.g. "EVIDENCE_MISSING: [topic]"
  reviewTier: ReviewTier;
}
