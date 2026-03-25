import { parseQuestionnaire, detectFormat } from '../parser.js';

describe('Questionnaire Parser', () => {
  describe('detectFormat()', () => {
    it('detects CSV when content has comma-separated header row', () => {
      const csv = 'id,question,category\nQ1,"Do you enforce MFA?",Access Control';
      expect(detectFormat(csv)).toBe('csv');
    });

    it('detects text when content is plain line-delimited questions', () => {
      const text = '1. Do you enforce MFA for all users?\n2. How often are access reviews performed?';
      expect(detectFormat(text)).toBe('text');
    });
  });

  describe('parseQuestionnaire()', () => {
    it('parses plain text question list with numbered lines', () => {
      const text = [
        '1. Do you enforce MFA for all users?',
        '2. How often are access reviews performed?',
        '3. Describe your incident response process.',
      ].join('\n');

      const questions = parseQuestionnaire(text, 'vendor-questionnaire.txt', 'text');

      expect(questions).toHaveLength(3);
      expect(questions[0]).toMatchObject({
        id: 'Q1',
        text: 'Do you enforce MFA for all users?',
        sourceDoc: 'vendor-questionnaire.txt',
      });
      expect(questions[2]).toMatchObject({
        id: 'Q3',
        text: 'Describe your incident response process.',
      });
    });

    it('parses CSV with id, question, and category columns', () => {
      const csv = [
        'id,question,category,framework',
        'AC-1,"Do you enforce MFA for all users?",Access Control,SOC2',
        'AC-2,"How often are access reviews performed?",Access Control,ISO27001',
        'IR-1,"Describe your incident response process.",Incident Response,',
      ].join('\n');

      const questions = parseQuestionnaire(csv, 'security-questionnaire.csv', 'csv');

      expect(questions).toHaveLength(3);
      expect(questions[0]).toMatchObject({
        id: 'AC-1',
        text: 'Do you enforce MFA for all users?',
        category: 'Access Control',
        framework: 'SOC2',
        sourceDoc: 'security-questionnaire.csv',
      });
      expect(questions[1]!.framework).toBe('ISO27001');
      expect(questions[2]!.category).toBe('Incident Response');
      expect(questions[2]!.framework).toBeNull();
    });
  });
});
