from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, StringConstraints


Identifier = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]
Strategy = Literal["rag", "agent"]
AgentAction = Literal["search_docs", "read_section", "finish", "insufficient_evidence"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IngestRequest(StrictModel):
    job_id: Identifier
    document_id: Identifier
    path: str = Field(min_length=1, max_length=4096)
    name: str = Field(min_length=1, max_length=255)
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")


class AnswerRequest(StrictModel):
    job_id: Identifier
    question: str = Field(min_length=1, max_length=4000)
    document_ids: list[Identifier] = Field(default_factory=list, max_length=200)
    strategy: Strategy = "rag"


class Citation(StrictModel):
    source_id: int = Field(default=0, ge=0, le=5)
    document_id: str
    document_name: str
    chunk_index: int
    excerpt: str
    score: float
    _has_adjacent: bool = PrivateAttr(default=False)


class EvidenceTrace(StrictModel):
    included_source_ids: list[int]
    omitted_source_ids: list[int]
    omitted_reason: str | None
    prompt_bytes: int
    byte_budget: int
    model_citation_ids: list[int]


class Generation(StrictModel):
    answer: str = Field(min_length=1, max_length=4000)
    citation_ids: list[int] = Field(max_length=5)
    insufficient_evidence: bool
    # Filled by the provider from the actual HTTP request/response, never by the model schema.
    _evidence_trace: EvidenceTrace | None = PrivateAttr(default=None)

    @property
    def evidence_trace(self) -> EvidenceTrace | None:
        return self._evidence_trace


class AgentDecision(StrictModel):
    action: AgentAction
    query: str = Field(default="", max_length=400, strict=True)
    source_id: int = Field(default=0, ge=0, strict=True)
    source_ids: list[Annotated[int, Field(gt=0, strict=True)]] = Field(default_factory=list, max_length=5)


class AgentStep(StrictModel):
    action: AgentAction
    outcome: Literal["ok", "empty", "duplicate", "invalid_arguments"]
    evidence_ids: list[int] = Field(default_factory=list)
    duration_ms: float


class AgentTrace(StrictModel):
    steps: list[AgentStep]
    stop_reason: Literal["finished", "insufficient_evidence", "step_limit"]
    selected_evidence_ids: list[int]


class AnswerResponse(StrictModel):
    answer: str
    citations: list[Citation]
    retrieval: list[Citation]
    evidence_trace: EvidenceTrace | None = None
    insufficient_evidence: bool
    model_mode: str
    timings: dict[str, float]
    strategy: Strategy = "rag"
    agent_trace: AgentTrace | None = None


class IngestResponse(StrictModel):
    document_id: str
    chunks_count: int
    embedding_signature: str
