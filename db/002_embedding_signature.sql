-- Idempotent upgrade for a previously initialized development database.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS embedding_signature TEXT NOT NULL DEFAULT '';
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS embedding_signature TEXT NOT NULL DEFAULT '';
-- Old vectors have unknown provenance. Reupload documents to reindex them;
-- never guess whether old vectors came from a mock or an actual embedding model.
