import asyncio

import httpx
import pytest

from app.config import Settings, get_settings
from app.main import app, provider_dependency, store_dependency
from app.models import AgentDecision, Citation, Generation
from app.providers import MockProvider


def chunk(document_id, index):
    citation = Citation(document_id=document_id, document_name=f"{document_id}.md", chunk_index=index,
                        excerpt=f"Evidence {index}", score=0.9)
    citation._has_adjacent = True
    return citation


class Store:
    def __init__(self):
        self.searches = []
        self.reads = []

    async def search(self, embedding, document_ids, *, include_adjacent=False):
        self.searches.append(document_ids)
        return [chunk("chosen", 0), chunk("outside_scope", 0)]

    async def read_section(self, source):
        self.reads.append((source.document_id, source.chunk_index))
        return [chunk(source.document_id, source.chunk_index + 1)]


class Planner(MockProvider):
    def __init__(self, actions):
        self.actions = iter(actions)

    async def choose(self, question, evidence, steps, previous_queries, allowed_actions):
        return AgentDecision.model_validate(next(self.actions))

    async def generate(self, question, citations):
        assert [part.chunk_index for part in citations] == [1, 0]
        return Generation(answer="Grounded conclusion [S1] [S2]", citation_ids=[1, 2], insufficient_evidence=False)


@pytest.fixture
async def client():
    app.dependency_overrides[get_settings] = lambda: Settings(internal_key="test-key")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


async def ask(client, store, provider):
    app.dependency_overrides[store_dependency] = lambda: store
    app.dependency_overrides[provider_dependency] = lambda: provider
    return await client.post("/internal/answer", headers={"X-Internal-Key": "test-key"},
                             json={"job_id": "job", "question": "Troubleshoot", "document_ids": ["chosen"],
                                   "strategy": "agent"})


@pytest.mark.asyncio
async def test_agent_reads_only_discovered_evidence_and_maps_final_citations(client):
    store = Store()
    provider = Planner([
        {"action": "search_docs", "query": "topic"},
        {"action": "read_section", "source_id": 2},
        {"action": "read_section", "source_id": 1},
        {"action": "finish", "source_ids": [2, 1]},
    ])
    response = await ask(client, store, provider)
    assert response.status_code == 200
    result = response.json()
    assert store.searches == [["chosen"]]
    assert store.reads == [("chosen", 0)]
    assert result["strategy"] == "agent"
    assert result["agent_trace"]["selected_evidence_ids"] == [2, 1]
    assert [step["outcome"] for step in result["agent_trace"]["steps"]] == ["ok", "invalid_arguments", "ok", "ok"]
    assert [(item["source_id"], item["chunk_index"]) for item in result["citations"]] == [(1, 1), (2, 0)]


@pytest.mark.asyncio
async def test_agent_budget_ends_repeated_queries_without_generating_an_answer(client):
    store = Store()
    provider = Planner([{"action": "search_docs", "query": query} for query in ["SSH", " ssh ", "Ssh", "ssh"]])
    response = await ask(client, store, provider)
    result = response.json()
    assert response.status_code == 200
    assert result["insufficient_evidence"] and result["citations"] == []
    assert result["agent_trace"]["stop_reason"] == "step_limit"
    assert [step["outcome"] for step in result["agent_trace"]["steps"]] == ["ok", "duplicate", "invalid_arguments", "invalid_arguments"]
    assert len(store.searches) == 1


@pytest.mark.asyncio
async def test_agent_wall_clock_timeout_cancels_planner(client, monkeypatch):
    cancelled = asyncio.Event()

    class SlowPlanner:
        async def choose(self, *args):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()

    monkeypatch.setattr("app.main.AGENT_TIMEOUT_SECONDS", 0.05)
    store = Store()
    response = await ask(client, store, SlowPlanner())
    assert response.status_code == 504 and response.json()["error"]["code"] == "agent_timeout"
    assert cancelled.is_set() and store.searches == []
