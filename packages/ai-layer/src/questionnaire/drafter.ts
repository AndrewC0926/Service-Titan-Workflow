import Anthropic from '@anthropic-ai/sdk';
import type {
  QuestionnaireQuestion,
  DraftedResponse,
  RetrievalResult,
  ReviewTier,
} from '@compliance-engine/types';
import { logger } from '@compliance-engine/common';
import { DRAFTER_SYSTEM_PROMPT } from './prompts/drafter.prompt.js';

const DEFAULT_MODEL = 'claude-sonnet-4-20250514';
const EVIDENCE_MISSING_PATTERN = /EVIDENCE_MISSING:\s*\[[^\]]+\]/g;

export interface DrafterConfig {
  anthropicApiKey: string;
  voyageApiKey: string;
  dbUrl: string;
  model?: string;
}

export interface DrafterDeps {
  retrieveContext: (query: string) => Promise<RetrievalResult[]>;
  injectContext: (results: RetrievalResult[]) => string;
}

/**
 * Draft a response to a questionnaire question using Claude API with RAG context.
 *
 * PROMPT INJECTION SAFETY:
 * - The system prompt is a static string imported from drafter.prompt.ts.
 *   It is NEVER modified or interpolated with external data.
 * - All external content (question text, RAG context) goes in the user
 *   turn only. This prevents adversarial content in questionnaires or
 *   policy documents from overriding system instructions.
 */
export async function draftResponse(
  question: QuestionnaireQuestion,
  deps: DrafterDeps,
  config: DrafterConfig,
): Promise<DraftedResponse> {
  // Retrieve RAG context for the question
  const retrievalResults = await deps.retrieveContext(question.text);
  const contextBlock = deps.injectContext(retrievalResults);

  // Build sources list from retrieval results
  const sources = retrievalResults.map((r) => r.chunk.sourceDoc);
  const uniqueSources = [...new Set(sources)];

  // Call Claude API
  // SECURITY: system prompt is the static DRAFTER_SYSTEM_PROMPT constant.
  // Question text and RAG context go in the user turn ONLY.
  const client = new Anthropic({ apiKey: config.anthropicApiKey });
  const model = config.model ?? DEFAULT_MODEL;

  const response = await client.messages.create({
    model,
    max_tokens: 1024,
    system: DRAFTER_SYSTEM_PROMPT,
    messages: [
      {
        role: 'user',
        content: buildUserMessage(question, contextBlock),
      },
    ],
  });

  const responseText = response.content
    .filter((block) => block.type === 'text')
    .map((block) => {
      if (block.type === 'text') return block.text;
      return '';
    })
    .join('\n');

  // Extract EVIDENCE_MISSING flags
  const flags = extractFlags(responseText);

  // Calculate confidence based on RAG result quality
  const confidence = calculateConfidence(retrievalResults, flags);

  // Assign review tier
  const reviewTier = assignReviewTier(confidence, flags);

  logger.info('Drafted response', {
    questionId: question.id,
    confidence,
    reviewTier,
    flagCount: flags.length,
    sourceCount: uniqueSources.length,
  });

  return {
    questionId: question.id,
    questionText: question.text,
    draftResponse: responseText,
    confidence,
    sources: uniqueSources,
    flags,
    reviewTier,
  };
}

/**
 * Build the user turn message. This is where ALL external content lives.
 * Never put any of this in the system prompt.
 */
function buildUserMessage(question: QuestionnaireQuestion, contextBlock: string): string {
  return [
    `Question ID: ${question.id}`,
    question.category ? `Category: ${question.category}` : null,
    question.framework ? `Framework: ${question.framework}` : null,
    '',
    `Question: ${question.text}`,
    '',
    contextBlock,
  ]
    .filter((line): line is string => line !== null)
    .join('\n');
}

/**
 * Extract EVIDENCE_MISSING flags from the drafted response text.
 */
function extractFlags(responseText: string): string[] {
  const matches = responseText.match(EVIDENCE_MISSING_PATTERN);
  return matches ?? [];
}

/**
 * Calculate confidence based on RAG retrieval quality.
 *   - 0 results → 0.0 (no evidence to ground claims)
 *   - 1-2 results → 0.6-0.8 based on average score
 *   - 3+ results → 0.85-1.0 based on average score
 */
function calculateConfidence(results: RetrievalResult[], flags: string[]): number {
  if (results.length === 0) return 0;

  const avgScore = results.reduce((sum, r) => sum + r.score, 0) / results.length;

  let confidence: number;
  if (results.length >= 3) {
    confidence = 0.85 + avgScore * 0.15;
  } else {
    confidence = 0.5 + avgScore * 0.3;
  }

  // Penalize for each flag
  confidence -= flags.length * 0.15;

  return Math.max(0, Math.min(1, Math.round(confidence * 100) / 100));
}

/**
 * Assign review tier based on confidence and flags.
 *   - confidence >= 0.85 AND no flags → 'auto_approve'
 *   - confidence >= 0.75 AND no flags → 'sme_review'
 *   - confidence < 0.75 OR any flags → 'legal_review'
 *   - confidence === 0 (no RAG results) → 'manual'
 */
function assignReviewTier(confidence: number, flags: string[]): ReviewTier {
  if (confidence === 0) return 'manual';
  if (flags.length > 0) return 'legal_review';
  if (confidence >= 0.85) return 'auto_approve';
  if (confidence >= 0.75) return 'sme_review';
  return 'legal_review';
}
