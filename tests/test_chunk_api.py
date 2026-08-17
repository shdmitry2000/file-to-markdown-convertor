"""Unit tests for the chunker registry + Docling HybridChunker wiring.

Tests deliberately bypass `app.api.main` (which binds ZeroMQ + loads all
converters at import time, ~30s cold start). They exercise the registry
directly + check the chunker plugin's class shape, which is what the new
/chunk endpoint dispatches to.

Integration test (real Docling pipeline) is marked and skipped by default.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def registry_after_import():
    """Import the chunkers package → runs @register_chunker decorators."""
    import app.chunkers  # noqa: F401
    from app.registry import registry
    return registry


def test_docling_hybrid_chunker_registered(registry_after_import):
    """Decorator on DoclingHybridChunkerImpl populates the registry."""
    caps = registry_after_import.get_chunker_capabilities()
    names = {c["name"] for c in caps["chunkers"]}
    assert "docling_hybrid" in names
    entry = next(c for c in caps["chunkers"] if c["name"] == "docling_hybrid")
    assert entry["label"] == "Docling · HybridChunker (Hebrew-optimized)"
    assert "Context-aware" in entry["description"]


def test_registry_returns_unknown_chunker_as_none(registry_after_import):
    """Unknown name → None (the API endpoint maps this to HTTP 400)."""
    assert registry_after_import.get_chunker("nonexistent_chunker") is None


def test_chunker_factory_produces_instance_with_chunk_method(registry_after_import):
    """get_chunker() returns an instance exposing the chunk(markdown, params) API."""
    impl = registry_after_import.get_chunker("docling_hybrid")
    assert impl is not None
    assert hasattr(impl, "chunk") and callable(impl.chunk)


def test_chunker_rejects_a_path_that_is_not_there():
    """This chunker takes a PDF PATH, not markdown — it converts the document
    itself rather than chunking text somebody else converted. The old contract
    ("empty markdown → empty list") no longer exists, and asserting it here meant
    the suite was describing an interface the code had stopped having."""
    from app.chunkers.docling_hybrid import DoclingHybridChunkerImpl

    impl = DoclingHybridChunkerImpl()
    with pytest.raises(FileNotFoundError):
        impl.chunk("/nonexistent/document.pdf", {"max_tokens": 128, "tokenizer": "x"})


def test_chunker_requires_the_settings_it_will_not_guess(tmp_path):
    """A missing knob is surfaced, never defaulted. A server-side default would
    silently diverge from the per-space setting it is supposed to honour, and the
    resulting chunk sizes would be wrong in a way nothing reports."""
    from app.chunkers.docling_hybrid import DoclingHybridChunkerImpl

    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    impl = DoclingHybridChunkerImpl()

    with pytest.raises(ValueError, match="max_tokens"):
        impl.chunk(str(pdf), {"tokenizer": "x"})
    with pytest.raises(ValueError, match="tokenizer"):
        impl.chunk(str(pdf), {"max_tokens": 128})


def test_chunker_capabilities_endpoint_shape(registry_after_import):
    """Shape contract for /chunk/capabilities — what v2's DoclingHybridChunker client expects."""
    caps = registry_after_import.get_chunker_capabilities()
    assert isinstance(caps, dict)
    assert "chunkers" in caps and isinstance(caps["chunkers"], list)
    for entry in caps["chunkers"]:
        assert {"name", "label", "description"}.issubset(entry.keys())


@pytest.mark.integration
def test_chunker_real_invocation_returns_chunks(sample_pdf):
    """Real Docling HybridChunker run over a real PDF.

    Deselected by default (see the `integration` marker in pyproject): it runs the
    docling pipeline and downloads a HuggingFace tokenizer, so it needs models and
    a network. The marker was never registered, so this ran in every suite — and
    it fed MARKDOWN to a chunker that takes a PDF path, meaning it could only ever
    fail. Run it deliberately with `pytest -m integration`.
    """
    from app.chunkers.docling_hybrid import DoclingHybridChunkerImpl

    impl = DoclingHybridChunkerImpl()
    try:
        chunks = impl.chunk(
            str(sample_pdf),
            {"max_tokens": 128, "tokenizer": "minishlab/potion-multilingual-128M"},
        )
    except RuntimeError as exc:
        pytest.skip(f"Docling dependencies missing: {exc}")
    assert len(chunks) > 0
    for c in chunks:
        assert "text" in c and isinstance(c["text"], str) and c["text"].strip()
