-- 002_pgvector.sql
-- Vector storage for RAG document chunks (voyage-3 embeddings)

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE document_chunks (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source_doc   TEXT NOT NULL,
  framework    framework,
  control_ids  TEXT[] DEFAULT '{}',
  chunk_index  INTEGER NOT NULL,
  text         TEXT NOT NULL,
  embedding    vector(1024),     -- voyage-3 output dimension
  ingested_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_chunks_embedding
  ON document_chunks USING ivfflat (embedding vector_cosine_ops)
  WITH (lists = 100);
