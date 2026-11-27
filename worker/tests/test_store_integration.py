"""Real pgvector integration. Opt in with PG_DSN_TEST; never silently mock SQL."""
import os
import uuid

import psycopg
import pytest

from app.config import Settings
from app.errors import WorkerError
from app.providers import mock_embedding
from app.store import PgChunkStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def database():
    dsn = os.getenv("PG_DSN_TEST")
    if not dsn:
        pytest.skip("Set PG_DSN_TEST to run real PostgreSQL/pgvector tests")
    token = uuid.uuid4().hex
    documents = [f"test_{token}_{i}" for i in range(3)]
    jobs = [f"job_{token}_{i}" for i in range(3)]
    async with await psycopg.AsyncConnection.connect(dsn) as connection:
        for i, (document, job) in enumerate(zip(documents, jobs)):
            await connection.execute(
                "INSERT INTO documents(id,name,sha256,status) VALUES(%s,%s,%s,'processing')",
                (document, f"integration_{i}.md", f"sha_{token}_{i}"),
            )
            await connection.execute(
                "INSERT INTO jobs(id,kind,status,payload) VALUES(%s,'ingest','running','{}'::jsonb)", (job,),
            )
        await connection.commit()
        try:
            yield connection, PgChunkStore(Settings(pg_dsn=dsn)), documents, jobs
        finally:
            # A test may have left its read-only transaction open; rollback before cleanup.
            await connection.rollback()
            await connection.execute("DELETE FROM chunks WHERE document_id = ANY(%s::text[])", (documents,))
            await connection.execute("DELETE FROM documents WHERE id = ANY(%s::text[])", (documents,))
            await connection.execute("DELETE FROM jobs WHERE id = ANY(%s::text[])", (jobs,))
            await connection.commit()


async def test_replacement_rolls_back_all_chunks_on_insert_failure(database):
    connection, store, documents, jobs = database
    await store.replace_chunks(jobs[0], documents[0], ["original RAII"], [mock_embedding("original RAII")])
    with pytest.raises(WorkerError) as caught:
        await store.replace_chunks(jobs[0], documents[0], ["first insert", None], [mock_embedding("new"), mock_embedding("new")])
    assert caught.value.code == "database_unavailable"
    cursor = await connection.execute("SELECT content,chunk_index FROM chunks WHERE document_id=%s ORDER BY chunk_index", (documents[0],))
    assert await cursor.fetchall() == [("original RAII", 0)]


async def test_exact_search_filters_scope_and_non_ready_documents(database):
    connection, store, documents, jobs = database
    for i in range(3):
        await store.replace_chunks(jobs[i], documents[i], [f"RAII lifetime evidence {i}"], [mock_embedding("RAII lifetime")])
    await connection.execute("UPDATE documents SET status='ready',embedding_signature='mock:featurehash-v1' WHERE id=ANY(%s::text[])", (documents[:2],))
    await connection.commit()
    scoped = await store.search(mock_embedding("RAII lifetime"), [documents[1]])
    assert len(scoped) == 1 and scoped[0].document_id == documents[1]
    assert scoped[0].score == pytest.approx(1.0)
    all_ready = await store.search(mock_embedding("RAII lifetime"), documents)
    assert {row.document_id for row in all_ready} == set(documents[:2])


async def test_section_read_is_limited_to_adjacent_chunks_of_ready_document(database):
    connection, store, documents, jobs = database
    for i in range(2):
        chunks = ["header", "needle", "adjacent", "beyond"]
        await store.replace_chunks(jobs[i], documents[i], chunks, [mock_embedding(chunk) for chunk in chunks])
    await connection.execute("UPDATE documents SET status='ready',embedding_signature='mock:featurehash-v1' WHERE id=ANY(%s::text[])",
                             (documents[:2],))
    await connection.commit()
    anchor = (await store.search(mock_embedding("needle"), [documents[0]], include_adjacent=True))[0]
    assert anchor._has_adjacent
    adjacent = await store.read_section(anchor)
    assert [(row.document_id, row.chunk_index) for row in adjacent] == [(documents[0], 0), (documents[0], 2)]
    await connection.execute("UPDATE documents SET status='failed' WHERE id=%s", (documents[0],))
    await connection.commit()
    assert await store.read_section(anchor) == []


async def test_late_ingestion_rejected_after_timeout(database):
    connection, store, documents, jobs = database
    await connection.execute("UPDATE jobs SET status='failed' WHERE id=%s", (jobs[0],))
    await connection.execute("UPDATE documents SET status='failed' WHERE id=%s", (documents[0],))
    await connection.commit()
    with pytest.raises(WorkerError) as caught:
        await store.replace_chunks(jobs[0], documents[0], ["late result"], [mock_embedding("late result")])
    assert caught.value.status_code == 409 and caught.value.code == "ingestion_cancelled"
    cursor = await connection.execute("SELECT count(*) FROM chunks WHERE document_id=%s", (documents[0],))
    assert (await cursor.fetchone())[0] == 0


async def test_switching_embedding_model_requires_reingestion(database):
    connection, store, documents, jobs = database
    await store.replace_chunks(jobs[0], documents[0], ["RAII lifetime"], [mock_embedding("RAII lifetime")])
    await connection.execute("UPDATE documents SET status='ready',embedding_signature='mock:featurehash-v1' WHERE id=%s", (documents[0],))
    await connection.commit()
    switched = PgChunkStore(Settings(pg_dsn=store.dsn, model_mode="ollama"))
    with pytest.raises(WorkerError) as caught:
        await switched.search(mock_embedding("RAII lifetime"), [documents[0]])
    assert caught.value.status_code == 409 and caught.value.code == "embedding_mismatch"
