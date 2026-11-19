import logging
import math

import psycopg
from psycopg.rows import dict_row

from .config import Settings
from .errors import WorkerError
from .models import Citation
from .providers import EMBEDDING_DIMENSION, embedding_signature

logger = logging.getLogger(__name__)


def vector_literal(vector: list[float]) -> str:
    if len(vector) != EMBEDDING_DIMENSION or not all(math.isfinite(v) for v in vector):
        raise WorkerError(502, "invalid_embedding", "Invalid embedding vector.")
    return "[" + ",".join(format(v, ".9g") for v in vector) + "]"


class PgChunkStore:
    """The worker only writes chunks. C++ owns documents, jobs and version updates."""

    def __init__(self, settings: Settings):
        self.dsn = settings.pg_dsn
        self.embedding_signature = embedding_signature(settings)

    async def _connect(self):
        return await psycopg.AsyncConnection.connect(
            self.dsn, connect_timeout=5, row_factory=dict_row,
            options="-c statement_timeout=30000 -c lock_timeout=5000",
        )

    async def replace_chunks(self, job_id: str, document_id: str, chunks: list[str], embeddings: list[list[float]]) -> None:
        if len(chunks) != len(embeddings):
            raise WorkerError(502, "invalid_embedding", "Embedding count does not match chunk count.")
        try:
            async with await self._connect() as connection:
                async with connection.transaction():
                    # Timeout and startup recovery lock the job before changing its status.
                    # Checking under that same lock prevents a late worker from publishing
                    # chunks after the backend has marked an ingestion failed.
                    cursor = await connection.execute("SELECT id FROM jobs WHERE id = %s AND status = 'running' FOR UPDATE", (job_id,))
                    if await cursor.fetchone() is None:
                        raise WorkerError(409, "ingestion_cancelled", "The ingestion task is no longer running.")
                    cursor = await connection.execute("SELECT id FROM documents WHERE id = %s AND status = 'processing' FOR UPDATE", (document_id,))
                    if await cursor.fetchone() is None:
                        raise WorkerError(409, "ingestion_cancelled", "The document is no longer processing.")
                    await connection.execute("DELETE FROM chunks WHERE document_id = %s", (document_id,))
                    async with connection.cursor() as cursor:
                        await cursor.executemany(
                            "INSERT INTO chunks(document_id, chunk_index, content, embedding, embedding_signature) VALUES (%s,%s,%s,%s::vector,%s)",
                            [(document_id, i, content, vector_literal(vector), self.embedding_signature)
                             for i, (content, vector) in enumerate(zip(chunks, embeddings))],
                        )
        except psycopg.Error as error:
            logger.warning("Database ingestion failed (%s)", type(error).__name__)
            raise WorkerError(503, "database_unavailable", "Document storage is unavailable.") from None

    async def search(self, embedding: list[float], document_ids: list[str]) -> list[Citation]:
        query_vector = vector_literal(embedding)
        filter_sql = " AND d.id = ANY(%s::text[])" if document_ids else ""
        try:
            async with await self._connect() as connection:
                # Check signatures and retrieve from one snapshot. Switching between
                # feature hashes and Ollama vectors must never compare incompatible spaces.
                await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                mismatch = await connection.execute(
                    "SELECT d.id FROM documents d WHERE d.status='ready' "
                    "AND d.embedding_signature <> %s" + filter_sql + " LIMIT 1",
                    (self.embedding_signature, document_ids) if document_ids else (self.embedding_signature,),
                )
                if await mismatch.fetchone() is not None:
                    raise WorkerError(409, "embedding_mismatch", "Selected documents use a different embedding model. Re-upload them in the current model mode.")
                cursor = await connection.execute(
                    "SELECT d.id AS document_id,d.name AS document_name,c.chunk_index,c.content,"
                    "1 - (c.embedding <=> %s::vector) AS score "
                    "FROM chunks c JOIN documents d ON d.id=c.document_id "
                    "WHERE d.status='ready' AND c.embedding_signature=%s" + filter_sql +
                    " ORDER BY c.embedding <=> %s::vector,c.document_id,c.chunk_index LIMIT 5",
                    # The filter appears before ORDER BY; match the SQL placeholder order.
                    (query_vector, self.embedding_signature, document_ids, query_vector) if document_ids else (query_vector, self.embedding_signature, query_vector),
                )
                records = await cursor.fetchall()
        except psycopg.Error as error:
            logger.warning("Database retrieval failed (%s)", type(error).__name__)
            raise WorkerError(503, "database_unavailable", "Document storage is unavailable.") from None
        return [Citation(
            document_id=row["document_id"], document_name=row["document_name"],
            chunk_index=row["chunk_index"], excerpt=row["content"],
            score=round(float(row["score"]), 6),
        ) for row in records]
