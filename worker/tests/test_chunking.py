import hashlib
import random

import pytest

from app.chunking import MAX_FILE_BYTES, chunk_text, read_document
from app.errors import WorkerError


def test_chunking_preserves_exact_source_and_bounds():
    rng = random.Random(42)
    text = "# 开发文档\n\n" + "".join(rng.choice("abc中文 \n#") for _ in range(6000)) + "\n结尾\n"
    chunks = chunk_text(text)
    assert all(100 < len(chunk) <= 800 for chunk in chunks[:-1])
    rebuilt = chunks[0] + "".join(chunk[100:] for chunk in chunks[1:])
    assert rebuilt == text
    assert all(left[-100:] == right[:100] for left, right in zip(chunks, chunks[1:]))


@pytest.mark.parametrize("text", ["single line", "你好", "a" * 4000, "\n# Heading\n" * 1000])
def test_long_paragraph_and_heading_inputs(text):
    chunks = chunk_text(text)
    assert max(map(len, chunks)) <= 800
    assert chunks[0] + "".join(chunk[100:] for chunk in chunks[1:]) == text


def test_empty_chunk_input_and_invalid_overlap():
    assert chunk_text(" \n\t") == []
    with pytest.raises(ValueError):
        chunk_text("text", overlap=800)


def document_file(tmp_path, content=b"# C++\nRAII ties resource lifetime to object lifetime."):
    path = tmp_path / "source.md"
    path.write_bytes(content)
    return path, hashlib.sha256(content).hexdigest()


def test_document_validates_hash_and_utf8(tmp_path):
    path, digest = document_file(tmp_path)
    assert "RAII" in read_document(str(path), digest, tmp_path)
    assert read_document(path.name, digest, tmp_path) == path.read_text()
    with pytest.raises(WorkerError) as caught:
        read_document(str(path), "0" * 64, tmp_path)
    assert caught.value.code == "document_hash_mismatch"


def test_traversal_rejected_before_read(tmp_path):
    root = tmp_path / "documents"
    root.mkdir()
    path, digest = document_file(tmp_path)
    for supplied in [str(path), "../source.md"]:
        with pytest.raises(WorkerError) as caught:
            read_document(supplied, digest, root)
        assert caught.value.code == "invalid_document_path"


def test_symlink_escape_rejected(tmp_path):
    root = tmp_path / "documents"
    root.mkdir()
    path, digest = document_file(tmp_path)
    link = root / "escape.md"
    try:
        link.symlink_to(path)
    except OSError:
        pytest.skip("Platform does not permit test symlinks")
    with pytest.raises(WorkerError) as caught:
        read_document(str(link), digest, root)
    assert caught.value.code == "invalid_document_path"


@pytest.mark.parametrize("content,code", [(b"\xff", "invalid_document_encoding"), (b" \n", "empty_document"), (b"a\x00b", "invalid_document_encoding")])
def test_invalid_document_contents(tmp_path, content, code):
    path, digest = document_file(tmp_path, content)
    with pytest.raises(WorkerError) as caught:
        read_document(str(path), digest, tmp_path)
    assert caught.value.code == code


def test_document_size_is_bounded(tmp_path):
    path, digest = document_file(tmp_path, b"a" * (MAX_FILE_BYTES + 1))
    with pytest.raises(WorkerError) as caught:
        read_document(str(path), digest, tmp_path)
    assert caught.value.status_code == 413


def test_file_extension_checked(tmp_path):
    path = tmp_path / "source.pdf"
    path.write_bytes(b"text")
    with pytest.raises(WorkerError) as caught:
        read_document(str(path), hashlib.sha256(b"text").hexdigest(), tmp_path)
    assert caught.value.code == "unsupported_document_type"
