CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    sha256 TEXT UNIQUE NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued','processing','ready','failed')),
    embedding_signature TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS chunks (
    id BIGSERIAL PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    embedding vector(1024) NOT NULL,
    embedding_signature TEXT NOT NULL DEFAULT '',
    UNIQUE(document_id,chunk_index)
);
CREATE INDEX IF NOT EXISTS chunks_document_id_idx ON chunks(document_id);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('ingest','question')),
    status TEXT NOT NULL CHECK (status IN ('queued','running','succeeded','failed')),
    payload JSONB NOT NULL,
    result JSONB,
    error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS jobs_status_created_idx ON jobs(status,created_at);
CREATE TABLE IF NOT EXISTS collection_state (
    id INTEGER PRIMARY KEY CHECK (id=1),
    version BIGINT NOT NULL DEFAULT 0
);
INSERT INTO collection_state(id,version) VALUES(1,0) ON CONFLICT(id) DO NOTHING;
