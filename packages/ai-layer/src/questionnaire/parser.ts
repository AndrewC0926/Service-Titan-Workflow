import type { QuestionnaireQuestion, Framework } from '@compliance-engine/types';

const VALID_FRAMEWORKS = new Set<string>(['SOC2', 'ISO27001', 'PCIDSS', 'ISO42001']);

/**
 * Detect whether content is CSV or plain text.
 * CSV is identified by a comma-separated first line with recognized header columns.
 */
export function detectFormat(content: string): 'text' | 'csv' {
  const firstLine = content.split('\n')[0] ?? '';
  const lower = firstLine.toLowerCase();
  if (lower.includes(',') && (lower.includes('question') || lower.includes('id'))) {
    return 'csv';
  }
  return 'text';
}

/**
 * Parse a questionnaire document into structured questions.
 * Supports plain text (numbered lines) and CSV (with header row).
 */
export function parseQuestionnaire(
  content: string,
  sourceDoc: string,
  format?: 'text' | 'csv',
): QuestionnaireQuestion[] {
  const fmt = format ?? detectFormat(content);
  return fmt === 'csv'
    ? parseCsv(content, sourceDoc)
    : parseText(content, sourceDoc);
}

/**
 * Parse numbered plain text lines: "1. Question text here"
 */
function parseText(content: string, sourceDoc: string): QuestionnaireQuestion[] {
  const lines = content.split('\n').filter((l) => l.trim().length > 0);
  const questions: QuestionnaireQuestion[] = [];

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]!.trim();
    // Match patterns like "1. Question" or "1) Question" or just "Question"
    const match = line.match(/^(\d+)[.)]\s*(.+)$/);
    const text = match ? match[2]!.trim() : line;
    const id = `Q${i + 1}`;

    questions.push({
      id,
      text,
      category: null,
      framework: null,
      sourceDoc,
    });
  }

  return questions;
}

/**
 * Parse CSV content with header row.
 * Expected columns: id, question, category (optional), framework (optional).
 * Handles quoted fields with commas inside.
 */
function parseCsv(content: string, sourceDoc: string): QuestionnaireQuestion[] {
  const lines = content.split('\n').filter((l) => l.trim().length > 0);
  if (lines.length < 2) return [];

  const headerLine = lines[0]!;
  const headers = parseCsvLine(headerLine).map((h) => h.toLowerCase().trim());

  const idIdx = headers.indexOf('id');
  const questionIdx = headers.indexOf('question');
  const categoryIdx = headers.indexOf('category');
  const frameworkIdx = headers.indexOf('framework');

  if (questionIdx === -1) {
    throw new Error('CSV must have a "question" column');
  }

  const questions: QuestionnaireQuestion[] = [];

  for (let i = 1; i < lines.length; i++) {
    const fields = parseCsvLine(lines[i]!);
    const id = idIdx !== -1 ? (fields[idIdx] ?? `Q${i}`) : `Q${i}`;
    const text = fields[questionIdx] ?? '';
    const category = categoryIdx !== -1 ? (fields[categoryIdx] || null) : null;
    const rawFramework = frameworkIdx !== -1 ? (fields[frameworkIdx] || null) : null;
    const framework = rawFramework && VALID_FRAMEWORKS.has(rawFramework)
      ? (rawFramework as Framework)
      : null;

    if (text.trim().length === 0) continue;

    questions.push({ id, text, category, framework, sourceDoc });
  }

  return questions;
}

/**
 * Parse a single CSV line respecting quoted fields.
 */
function parseCsvLine(line: string): string[] {
  const fields: string[] = [];
  let current = '';
  let inQuotes = false;

  for (let i = 0; i < line.length; i++) {
    const char = line[i]!;
    if (char === '"') {
      inQuotes = !inQuotes;
    } else if (char === ',' && !inQuotes) {
      fields.push(current.trim());
      current = '';
    } else {
      current += char;
    }
  }

  fields.push(current.trim());
  return fields;
}
