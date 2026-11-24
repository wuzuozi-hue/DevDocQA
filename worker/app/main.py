import hmac
import logging
import re
from time import perf_counter
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .cancellation import while_connected
from .chunking import chunk_text, read_document
from .config import Settings, get_settings
from .errors import WorkerError
from .models import AnswerRequest, AnswerResponse, IngestRequest, IngestResponse
from .providers import get_provider
from .store import PgChunkStore

logger = logging.getLogger(__name__)
app = FastAPI(title="DevDocQA internal worker", version="0.1.0", docs_url=None, redoc_url=None)


@app.exception_handler(WorkerError)
async def worker_error_handler(request: Request, error: WorkerError):
    return JSONResponse(status_code=error.status_code, content={"error": {"code": error.code, "message": error.message}})


@app.exception_handler(RequestValidationError)
async def request_error_handler(request: Request, error: RequestValidationError):
    return JSONResponse(status_code=422, content={"error": {"code": "invalid_request", "message": "Request does not match the required schema."}})


def require_internal_key(
    settings: Annotated[Settings, Depends(get_settings)],
    x_internal_key: Annotated[str | None, Header()] = None,
) -> None:
    if not settings.internal_key:
        raise WorkerError(503, "internal_key_unconfigured", "The worker internal key is not configured.")
    if x_internal_key is None or not hmac.compare_digest(x_internal_key.encode(), settings.internal_key.encode()):
        raise WorkerError(401, "unauthorized", "A valid internal key is required.")


def store_dependency(settings: Annotated[Settings, Depends(get_settings)]):
    return PgChunkStore(settings)


def provider_dependency(settings: Annotated[Settings, Depends(get_settings)]):
    return get_provider(settings)


@app.get("/health")
async def health():
    # Liveness is intentionally independent of database/model readiness.
    return {"status": "ok", "service": "devdocqa-worker"}


@app.post("/internal/ingest", response_model=IngestResponse, dependencies=[Depends(require_internal_key)])
async def ingest(
    payload: IngestRequest,
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    store=Depends(store_dependency),
    provider=Depends(provider_dependency),
):
    # Files are bounded to 2 MB. Isolate filesystem access and hashing from the event loop.
    from starlette.concurrency import run_in_threadpool
    text = await run_in_threadpool(read_document, payload.path, payload.sha256, settings.data_dir)
    chunks = await run_in_threadpool(chunk_text, text)
    async def process():
        embeddings = await provider.embed(chunks)
        await store.replace_chunks(payload.job_id, payload.document_id, chunks, embeddings)
    await while_connected(request, process())
    logger.info("Ingestion succeeded job_id=%s document_id=%s chunks=%s", payload.job_id, payload.document_id, len(chunks))
    return IngestResponse(document_id=payload.document_id, chunks_count=len(chunks), embedding_signature=provider.embedding_signature)


@app.post("/internal/answer", response_model=AnswerResponse, dependencies=[Depends(require_internal_key)])
async def answer(payload: AnswerRequest, request: Request, store=Depends(store_dependency), provider=Depends(provider_dependency)):
    return await while_connected(request, generate_answer(payload, store, provider))


async def generate_answer(payload: AnswerRequest, store, provider):
    question = payload.question.strip()
    if not question:
        raise WorkerError(422, "empty_question", "Question must contain non-whitespace characters.")
    start = perf_counter()
    embeddings = await provider.embed([question])
    after_embedding = perf_counter()
    retrieved = await store.search(embeddings[0], list(dict.fromkeys(payload.document_ids)))
    citations = [citation.model_copy(update={"source_id": index}) for index, citation in enumerate(retrieved, 1)]
    after_search = perf_counter()
    generation = await provider.generate(question, citations)
    used_ids = list(dict.fromkeys(generation.citation_ids))
    answer_text = generation.answer
    inline_ids = {int(value) for value in re.findall(r"\[S(\d+)\]", answer_text)}
    if any(identifier < 1 or identifier > len(citations) for identifier in used_ids) or not inline_ids.issubset(set(used_ids)):
        raise WorkerError(502, "invalid_model_citation", "The model referenced evidence that was not retrieved.")
    if used_ids and not inline_ids:
        # Structured IDs are declared references, not proof of factual support.
        answer_text += "\n\n参考片段：" + " ".join(f"[S{identifier}]" for identifier in used_ids)
        inline_ids = set(used_ids)
    if not generation.insufficient_evidence and (not used_ids or not inline_ids):
        raise WorkerError(502, "missing_model_citation", "The model answer does not cite retrieved evidence.")
    # A model may list extra retrieved IDs in JSON without actually citing them.
    # Publish only IDs referenced in the answer, never uncited search candidates.
    used_ids = [identifier for identifier in used_ids if identifier in inline_ids]
    elapsed = perf_counter()
    logger.info("Answer completed job_id=%s mode=%s elapsed_ms=%.1f", payload.job_id, provider.mode, (elapsed-start)*1000)
    return AnswerResponse(
        answer=answer_text,
        citations=[citations[identifier - 1] for identifier in used_ids],
        retrieval=citations,
        evidence_trace=generation.evidence_trace,
        insufficient_evidence=generation.insufficient_evidence,
        model_mode=provider.mode,
        timings={"embedding_ms": round((after_embedding-start)*1000, 3),
                 "retrieval_ms": round((after_search-after_embedding)*1000, 3),
                 "generation_ms": round((elapsed-after_search)*1000, 3),
                 "total_ms": round((elapsed-start)*1000, 3)},
    )
