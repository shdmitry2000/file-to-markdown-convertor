"""Docling HybridChunker — context-aware chunking with Hebrew support.

Processes PDFs directly: PDF → DoclingDocument → Chunks (no markdown intermediary).
Uses proper docling_core API with Hebrew-compatible tokenizer and markdown table
serialization for better embedding quality.

Pipeline:
  1. PDF → DocumentConverter → DoclingDocument (with structure metadata)
  2. Configure HybridChunker with Hebrew tokenizer + markdown table serializer
  3. Chunk DoclingDocument → structured chunks with heading_path, page, token_count

Params (all read from the caller's per-space settings — no server-side
defaults so the configured value can't silently drift):
  max_tokens: int (REQUIRED)   — Target chunk size in tokens
  tokenizer:  str (REQUIRED)   — HuggingFace tokenizer model id
  merge_peers: bool = True     — Merge small adjacent chunks (the only knob
                                  with a server-side default)

Runs in the markdown-api `chunk_server` over ZeroMQ REQ/ROUTER (port 5557).
v2's DoclingHybridChunker plugin sends `max_tokens` from chunker.chunk_size
and `tokenizer` from chunker.params.tokenizer.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.chunkers.base import Chunker
from app.registry import register_chunker

logger = logging.getLogger(__name__)


@register_chunker(
    name="docling_hybrid",
    label="Docling · HybridChunker (Hebrew-optimized)",
    description=(
        "Context-aware chunking via Docling's HybridChunker. Processes PDFs directly, "
        "preserves document structure (heading hierarchy, table boundaries). Uses Hebrew-compatible "
        "tokenizer (potion-multilingual-128M, 8K tokens) and markdown table serialization. "
        "Recommended for multilingual documents."
    ),
)
class DoclingHybridChunkerImpl(Chunker):
    def chunk(self, file_path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Chunk PDF directly without markdown conversion.
        
        Args:
            file_path: Path to PDF file
            params: {
                "max_tokens": <int>,       # REQUIRED — caller sets from settings
                "tokenizer":  <str>,       # REQUIRED — HuggingFace tokenizer id
                "merge_peers": True,       # optional, default True
            }
        
        Returns:
            List of chunks with text, metadata (heading_path, page, token_count)
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        # All params must come from the caller (which reads them from per-
        # space settings). No silent fallbacks — a missing knob is a bug we
        # want to surface loudly, not paper over with a hardcoded default
        # that drifts from the user's configuration.
        try:
            max_tokens = int(params["max_tokens"])
        except KeyError as exc:
            raise ValueError(
                "docling_hybrid: 'max_tokens' is required in params (set "
                "chunker.chunk_size in the per-space settings)"
            ) from exc
        try:
            tokenizer_id = params["tokenizer"]
        except KeyError as exc:
            raise ValueError(
                "docling_hybrid: 'tokenizer' is required in params (set "
                "chunker.params.tokenizer in the per-space settings, "
                "e.g. 'minishlab/potion-multilingual-128M' for Hebrew)"
            ) from exc
        merge_peers = bool(params.get("merge_peers", True))
        
        # Late import: keep registration decorator cheap on cold start
        try:
            from docling.document_converter import DocumentConverter
            from docling_core.transforms.chunker.hybrid_chunker import HybridChunker
            from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer
            from docling_core.transforms.chunker.hierarchical_chunker import (
                ChunkingDocSerializer,
                ChunkingSerializerProvider,
            )
            from docling_core.transforms.serializer.markdown import MarkdownTableSerializer
            from transformers import AutoTokenizer
        except ImportError as exc:
            raise RuntimeError(
                f"DoclingHybridChunker dependencies not installed: {exc}. "
                "Need docling, docling-core, transformers."
            )
        
        # Step 1: PDF → DoclingDocument
        logger.info(f"Converting PDF to DoclingDocument: {file_path}")
        converter = DocumentConverter()
        result = converter.convert(str(path))
        dl_doc = result.document
        logger.info(f"Converted PDF: {dl_doc.num_pages()} pages")
        
        # Step 2: Tokenizer setup (8K token support for up to 4K chunks)
        logger.info(f"Loading tokenizer: {tokenizer_id}")
        hf_tokenizer = AutoTokenizer.from_pretrained(tokenizer_id)
        tokenizer = HuggingFaceTokenizer(tokenizer=hf_tokenizer, max_tokens=max_tokens)
        
        # Step 3: Hebrew-optimized serializer (markdown tables for better embeddings)
        class HebrewOptimizedSerializerProvider(ChunkingSerializerProvider):
            """Markdown table serialization for better Hebrew embedding quality."""
            def get_serializer(self, doc):
                return ChunkingDocSerializer(
                    doc=doc,
                    table_serializer=MarkdownTableSerializer(),
                )
        
        # Step 4: HybridChunker with Hebrew-optimized serialization
        logger.info(f"Chunking with max_tokens={max_tokens}, merge_peers={merge_peers}")
        chunker = HybridChunker(
            tokenizer=tokenizer,
            max_tokens=max_tokens,
            merge_peers=merge_peers,
            serializer_provider=HebrewOptimizedSerializerProvider(),
        )
        
        # Step 5: Chunk and extract metadata
        out: list[dict[str, Any]] = []
        for i, chunk in enumerate(chunker.chunk(dl_doc=dl_doc)):
            entry = {
                "text": chunk.text,
                "index": i,
                "metadata": {}
            }
            
            # Extract metadata
            if chunk.meta:
                # Heading path
                if chunk.meta.headings:
                    entry["metadata"]["heading_path"] = list(chunk.meta.headings)
                
                # Page number
                if hasattr(chunk.meta, "doc_items") and chunk.meta.doc_items:
                    for item in chunk.meta.doc_items:
                        if hasattr(item, "prov") and item.prov:
                            for prov in item.prov:
                                if hasattr(prov, "page_no") and prov.page_no is not None:
                                    entry["metadata"]["page"] = int(prov.page_no)
                                    break
                            if "page" in entry["metadata"]:
                                break
            
            # Token count
            try:
                entry["metadata"]["token_count"] = len(
                    hf_tokenizer.encode(chunk.text, add_special_tokens=False)
                )
            except Exception:
                pass
            
            # Contextualized text (for embeddings with heading context)
            try:
                ctx_text = chunker.contextualize(chunk=chunk)
                if ctx_text and ctx_text != chunk.text:
                    entry["contextualized_text"] = ctx_text
            except Exception:
                pass
            
            out.append(entry)
        
        logger.info(f"Successfully chunked {file_path} into {len(out)} chunks")
        return out
