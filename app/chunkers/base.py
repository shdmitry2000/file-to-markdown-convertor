"""Abstract base for markdown-api chunkers.

A chunker takes markdown text + params and returns a list of chunk dicts:
    [
        {
          "text": "...",
          "heading_path": ["H1", "H2"],   # optional
          "token_count": 487,             # optional
          "page": 3,                      # optional
          "metadata": {...},              # optional
        },
        ...
    ]

Synchronous (chunking is fast vs PDF conversion; no need for ZeroMQ).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Chunker(ABC):
    """Base interface for chunkers."""

    @abstractmethod
    def chunk(self, markdown: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Split markdown into chunks. Implementations control how `params` is used."""
