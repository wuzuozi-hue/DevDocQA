from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, StringConstraints


Identifier = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]


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


class Citation(StrictModel):
    source_id: int = Field(default=0, ge=0, le=5)
    document_id: str
    document_name: str
    chunk_index: int
    excerpt: str
    score: float


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


class AnswerResponse(StrictModel):
    answer: str
    citations: list[Citation]
    retrieval: list[Citation]
    evidence_trace: EvidenceTrace | None = None
    insufficient_evidence: bool
    model_mode: str
    timings: dict[str, float]


class IngestResponse(StrictModel):
    document_id: str
    chunks_count: int
    embedding_signature: str
