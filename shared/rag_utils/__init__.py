"""Shared utilities for RAG templates.

Cross-template helpers that aren't specific to any one storage backend or
retrieval strategy. New code that's used by 2+ templates belongs here so we
have a single source of truth for evaluator-facing contracts.
"""

from .metadata import ensure_source_metadata, basename_or_empty

__all__ = ["ensure_source_metadata", "basename_or_empty"]
