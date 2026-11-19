import hashlib

import httpx
import pytest

from app.config import Settings, get_settings
from app.errors import WorkerError
from app.main import app, provider_dependency, store_dependency
from app.models import Citation, Generation
from app.providers import MockProvider


class FakeStore:
    def __init__(self):
        self.ingested = []
        self.retrieved_ids = []
        self.fail = False

    async def replace_chunks(self, job_id, document_id, chunks, embeddings):
        if self.fail:
            raise WorkerError(503, "database_unavailable", "Document storage is unavailable.")
        self.ingested.append((job_id, document_id, chunks, embeddings))

    async def search(self, embedding, document_ids):
        self.retrieved_ids = document_ids
        if self.fail:
            raise WorkerError(503, "database_unavailable", "Document storage is unavailable.")
        return [Citation(document_id="doc", document_name="raii.md", chunk_index=0, excerpt="RAII lifetime arr[0]", score=.9)]


@pytest.fixture
def deps(tmp_path):
    settings = Settings(internal_key="test-key", data_dir=tmp_path)
    store = FakeStore()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[store_dependency] = lambda: store
    app.dependency_overrides[provider_dependency] = lambda: MockProvider()
    yield settings, store
    app.dependency_overrides.clear()


@pytest.fixture
async def client(deps):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as value:
        yield value


HEADERS = {"X-Internal-Key": "test-key"}


@pytest.mark.asyncio
async def test_health_and_auth(client):
    assert (await client.get("/health")).status_code == 200
    for headers in [{}, {"X-Internal-Key": "wrong"}]:
        response = await client.post("/internal/answer", json={"job_id": "job", "question": "RAII"}, headers=headers)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.asyncio
async def test_ingestion_validates_before_persistence(client, deps):
    settings, store = deps
    path = settings.data_dir / "raii.md"
    content = b"# RAII\nResource lifetime is tied to object lifetime."
    path.write_bytes(content)
    payload = {"job_id": "job", "document_id": "doc", "path": str(path), "name": "raii.md", "sha256": hashlib.sha256(content).hexdigest()}
    response = await client.post("/internal/ingest", json=payload, headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {"document_id": "doc", "chunks_count": 1, "embedding_signature": "mock:featurehash-v1"}
    assert store.ingested[0][0:2] == ("job", "doc")
    assert len(store.ingested[0][3][0]) == 1024
    payload["sha256"] = "0" * 64
    response = await client.post("/internal/ingest", json=payload, headers=HEADERS)
    assert response.status_code == 422
    assert len(store.ingested) == 1


@pytest.mark.asyncio
async def test_answer_returns_citations_timings_and_filtered_ids(client, deps):
    response = await client.post("/internal/answer", json={"job_id": "job", "question": "RAII", "document_ids": ["doc", "doc"]}, headers=HEADERS)
    assert response.status_code == 200
    result = response.json()
    assert result["model_mode"] == "mock" and not result["insufficient_evidence"]
    assert "模拟模式" in result["answer"] and "[S1]" in result["answer"]
    assert result["citations"][0]["excerpt"] == "RAII lifetime arr[0]"
    assert result["retrieval"] == result["citations"]
    assert result["retrieval"][0]["source_id"] == 1
    assert all(value >= 0 for value in result["timings"].values())
    assert deps[1].retrieved_ids == ["doc"]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"job_id": "job", "question": ""}, {"job_id": "job", "question": "a"*4001}, {"job_id": "job", "question": "ok", "document_ids": ["doc';DROP TABLE chunks;"]}, {"job_id": "job", "question": "ok", "unexpected": True}])
async def test_invalid_request_schema(client, payload):
    response = await client.post("/internal/answer", json=payload, headers=HEADERS)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_whitespace_and_database_failure(client, deps):
    response = await client.post("/internal/answer", json={"job_id": "job", "question": "  "}, headers=HEADERS)
    assert response.json()["error"]["code"] == "empty_question"
    deps[1].fail = True
    response = await client.post("/internal/answer", json={"job_id": "job", "question": "RAII"}, headers=HEADERS)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("generation,expected", [
    (Generation(answer="invented [S8]", citation_ids=[8], insufficient_evidence=False), "invalid_model_citation"),
    (Generation(answer="invented [S8]", citation_ids=[1], insufficient_evidence=False), "invalid_model_citation"),
    (Generation(answer="uncited answer", citation_ids=[], insufficient_evidence=False), "missing_model_citation"),
])
async def test_model_cannot_invent_citation_ids(client, generation, expected):
    class InvalidProvider(MockProvider):
        async def generate(self, question, citations):
            return generation
    app.dependency_overrides[provider_dependency] = lambda: InvalidProvider()
    response = await client.post("/internal/answer", json={"job_id": "job", "question": "RAII"}, headers=HEADERS)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == expected


@pytest.mark.asyncio
async def test_retrieval_preserved_when_generation_has_no_citations(client):
    class InsufficientProvider(MockProvider):
        async def generate(self, question, citations):
            return Generation(answer="资料不足", citation_ids=[], insufficient_evidence=True)
    app.dependency_overrides[provider_dependency] = lambda: InsufficientProvider()
    response = await client.post("/internal/answer", json={"job_id": "job", "question": "unknown"}, headers=HEADERS)
    assert response.status_code == 200
    result = response.json()
    assert result["citations"] == []
    assert result["retrieval"][0]["document_id"] == "doc"
    assert result["retrieval"][0]["excerpt"] == "RAII lifetime arr[0]"


@pytest.mark.asyncio
async def test_structured_valid_reference_gets_display_marker(client):
    class StructuredProvider(MockProvider):
        async def generate(self, question, citations):
            return Generation(answer="RAII 使用析构函数释放资源。", citation_ids=[1], insufficient_evidence=False)
    app.dependency_overrides[provider_dependency] = lambda: StructuredProvider()
    response = await client.post("/internal/answer", json={"job_id": "job", "question": "RAII"}, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["answer"].endswith("参考片段：[S1]")
    assert response.json()["citations"][0]["document_id"] == "doc"


@pytest.mark.asyncio
async def test_filtered_citation_preserves_original_source_number(client):
    class TwoSourceStore(FakeStore):
        async def search(self, embedding, document_ids):
            return [Citation(document_id=f"doc{i}", document_name=f"doc{i}.md", chunk_index=0, excerpt="source", score=.5) for i in range(2)]
    class SecondSourceProvider(MockProvider):
        async def generate(self, question, citations):
            return Generation(answer="结论 [S2]", citation_ids=[2], insufficient_evidence=False)
    app.dependency_overrides[store_dependency] = lambda: TwoSourceStore()
    app.dependency_overrides[provider_dependency] = lambda: SecondSourceProvider()
    response = await client.post("/internal/answer", json={"job_id": "job", "question": "source"}, headers=HEADERS)
    result = response.json()
    assert [citation["source_id"] for citation in result["retrieval"]] == [1, 2]
    assert len(result["citations"]) == 1
    assert result["citations"][0]["source_id"] == 2
    assert result["citations"][0]["document_id"] == "doc1"
