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
    assert entry["label"] == "Docling HybridChunker"
    assert "Context-aware" in entry["description"]


def test_registry_returns_unknown_chunker_as_none(registry_after_import):
    """Unknown name → None (the API endpoint maps this to HTTP 400)."""
    assert registry_after_import.get_chunker("nonexistent_chunker") is None


def test_chunker_factory_produces_instance_with_chunk_method(registry_after_import):
    """get_chunker() returns an instance exposing the chunk(markdown, params) API."""
    impl = registry_after_import.get_chunker("docling_hybrid")
    assert impl is not None
    assert hasattr(impl, "chunk") and callable(impl.chunk)


def test_chunker_empty_markdown_short_circuits():
    """Empty input → empty list, without importing Docling."""
    from app.chunkers.docling_hybrid import DoclingHybridChunkerImpl
    impl = DoclingHybridChunkerImpl()
    assert impl.chunk("", {}) == []


def test_chunker_capabilities_endpoint_shape(registry_after_import):
    """Shape contract for /chunk/capabilities — what v2's DoclingHybridChunker client expects."""
    caps = registry_after_import.get_chunker_capabilities()
    assert isinstance(caps, dict)
    assert "chunkers" in caps and isinstance(caps["chunkers"], list)
    for entry in caps["chunkers"]:
        assert {"name", "label", "description"}.issubset(entry.keys())


@pytest.mark.integration
def test_chunker_real_invocation_returns_chunks():
    """Real Docling HybridChunker run. Requires docling + transformers installed."""
    from app.chunkers.docling_hybrid import DoclingHybridChunkerImpl
    impl = DoclingHybridChunkerImpl()
    markdown = (
        "# Sample Document\n\n"
        "## Introduction\n\n"
        "RAG combines retrieval with generation. The retrieval step pulls "
        "context from a knowledge base.\n\n"
        "## Methods\n\n"
        "Common patterns include reranking, query expansion, and hybrid search.\n"
    )
    try:
        chunks = impl.chunk(markdown, {"max_tokens": 128})
    except RuntimeError as exc:
        pytest.skip(f"Docling dependencies missing: {exc}")
    assert len(chunks) > 0
    for c in chunks:
        assert "text" in c and isinstance(c["text"], str) and c["text"].strip()
