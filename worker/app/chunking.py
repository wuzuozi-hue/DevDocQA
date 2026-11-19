"""Bounded chunks that retain every source character, including whitespace.

The next chunk starts 100 characters before the previous boundary. Heading and
paragraph boundaries are preferred, but very long paragraphs are split too.
"""
import hashlib
from pathlib import Path

from .errors import WorkerError

MAX_FILE_BYTES = 2 * 1024 * 1024


def read_document(path_text: str, expected_hash: str, data_dir: Path) -> str:
    root = data_dir.resolve()
    supplied = Path(path_text)
    try:
        path = (supplied if supplied.is_absolute() else root / supplied).resolve()
        path.relative_to(root)
    except (ValueError, OSError):
        raise WorkerError(422, "invalid_document_path", "Document path must stay inside DATA_DIR.") from None
    if path.suffix.lower() not in {".md", ".txt"}:
        raise WorkerError(422, "unsupported_document_type", "Only Markdown and UTF-8 text files are supported.")
    try:
        # A bounded read prevents a file changed after stat() from allocating unbounded memory.
        with path.open("rb") as source:
            data = source.read(MAX_FILE_BYTES + 1)
    except (OSError, ValueError):
        raise WorkerError(422, "document_unavailable", "The document could not be read.") from None
    if len(data) > MAX_FILE_BYTES:
        raise WorkerError(413, "document_too_large", "Document exceeds the 2 MB limit.")
    if hashlib.sha256(data).hexdigest() != expected_hash.lower():
        raise WorkerError(422, "document_hash_mismatch", "Document SHA-256 does not match the submitted content.")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise WorkerError(422, "invalid_document_encoding", "Document must be UTF-8 text.") from None
    if not text.strip():
        raise WorkerError(422, "empty_document", "Document is empty.")
    if "\x00" in text:
        raise WorkerError(422, "invalid_document_encoding", "Document contains NUL characters.")
    return text


def chunk_text(text: str, max_chars: int = 800, overlap: int = 100) -> list[str]:
    if not 0 <= overlap < max_chars or max_chars < 2:
        raise ValueError("Require 0 <= overlap < max_chars and max_chars >= 2")
    if not text.strip():
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            # Avoid tiny chunks and make progress beyond the overlap window.
            floor = start + max(overlap + 1, max_chars // 2)
            paragraph = text.rfind("\n\n", floor, end)
            heading = text.rfind("\n#", floor, end)
            boundary = max(paragraph + 2 if paragraph >= 0 else -1,
                           heading + 1 if heading >= 0 else -1)
            if boundary > floor:
                end = boundary
        chunks.append(text[start:end])
        if end == len(text):
            break
        start = end - overlap
    return chunks
