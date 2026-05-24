"""Docling HybridChunker — context-aware token-aware chunking of markdown.

Pipeline:
  1. Write incoming markdown to a temp .md file (Docling needs a path or stream).
  2. Run DocumentConverter to get a DoclingDocument with structural metadata
     (headings, paragraphs, tables).
  3. Run HybridChunker(tokenizer, max_tokens, merge_peers) over the document.
  4. For each Chonkie/Docling chunk, extract text + heading_path + token_count.

Params (POST /chunk body.params):
  max_tokens: int = 512        — HybridChunker target chunk size in tokens
  merge_peers: bool = True     — merge small adjacent chunks
  tokenizer:   str = "sentence-transformers/all-MiniLM-L6-v2" — for token counting

This chunker runs in the markdown-api process (Docling + HF weights live here,
pre-downloaded in the Docker image). v2's DoclingHybridChunker plugin is just
an HTTP client to this endpoint.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

from app.chunkers.base import Chunker
from app.registry import register_chunker

logger = logging.getLogger(__name__)


@register_chunker(
    name="docling_hybrid",
    label="Docling HybridChunker",
    description=(
        "Context-aware token-aware chunking. Preserves heading hierarchy, table "
        "boundaries, and section structure via Docling's DocumentConverter + "
        "HybridChunker. Token-counted via configurable tokenizer."
    ),
)
class DoclingHybridChunkerImpl(Chunker):
    def chunk(self, markdown: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if not markdown:
            return []

        max_tokens = int(params.get("max_tokens", 512))
        merge_peers = bool(params.get("merge_peers", True))
        tokenizer_id = params.get("tokenizer", "sentence-transformers/all-MiniLM-L6-v2")

        # Late import: keep the registration decorator cheap on cold start.
        try:
            from docling.chunking import HybridChunker
            from docling.document_converter import DocumentConverter
            from transformers import AutoTokenizer
        except ImportError as exc:
            raise RuntimeError(
                f"DoclingHybridChunker dependencies not installed: {exc}. "
                "Need docling + transformers."
            )

        # Step 1+2: markdown → DoclingDocument (write to temp file so Docling can ingest).
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".md", delete=False,
        ) as tmp:
            tmp.write(markdown)
            tmp_path = Path(tmp.name)
        try:
            converter = DocumentConverter()
            result = converter.convert(str(tmp_path))
            dl_doc = result.document
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass

        # Step 3: tokenizer + chunker.
        tokenizer = AutoTokenizer.from_pretrained(tokenizer_id)
        chunker = HybridChunker(
            tokenizer=tokenizer,
            max_tokens=max_tokens,
            merge_peers=merge_peers,
        )

        # Step 4: produce chunks with metadata.
        out: list[dict[str, Any]] = []
        for raw in chunker.chunk(dl_doc=dl_doc):
            entry: dict[str, Any] = {"text": getattr(raw, "text", "") or ""}
            # Docling HybridChunker chunks expose `.meta` (ChunkMeta) with headings.
            meta = getattr(raw, "meta", None)
            if meta is not None:
                headings = getattr(meta, "headings", None)
                if headings:
                    entry["heading_path"] = list(headings)
                page_no = _first_page(meta)
                if page_no is not None:
                    entry["page"] = page_no
            # contextualize() prepends heading context — useful for embedding;
            # we surface it as a sibling field so callers can choose.
            try:
                ctxd = chunker.contextualize(chunk=raw)
                if ctxd and ctxd != entry["text"]:
                    entry["contextualized_text"] = ctxd
            except Exception:
                pass
            # Token count (best-effort using same tokenizer).
            try:
                entry["token_count"] = len(tokenizer.encode(entry["text"], add_special_tokens=False))
            except Exception:
                pass
            out.append(entry)
        return out


def _first_page(meta: Any) -> int | None:
    """Best-effort page-number extraction from Docling chunk meta."""
    try:
        items = getattr(meta, "doc_items", None) or []
        for item in items:
            for prov in getattr(item, "prov", []) or []:
                page_no = getattr(prov, "page_no", None)
                if page_no is not None:
                    return int(page_no)
    except Exception:
        pass
    return None
