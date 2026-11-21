"""Single source of truth for the evaluator-facing metadata contract.

Every RAG template returns search results whose ``metadata`` dict is consumed
by the evaluator's ``UnifiedMatcher``. The matcher looks for ``source`` /
``source_file`` to do file-level matching against golden ground truth. Each
template historically reinvented this logic with subtle differences, leading
to the F1=0 issues we fixed system by system.

This module provides one canonical helper. Adopt it from any template's
``_to_search_result``-equivalent code path.

Usage::

    from shared.rag_utils import ensure_source_metadata
    metadata = ensure_source_metadata(metadata, fallbacks=[md_file_name])
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, Optional


def basename_or_empty(value: Optional[str]) -> str:
    """Return ``os.path.basename(value)`` if ``value`` is non-empty, else ``""``.

    Defensive against ``None`` and empty strings — calling code can pass any
    of the candidate signals without pre-filtering.
    """
    return os.path.basename(value) if value else ""


def ensure_source_metadata(
    metadata: Dict[str, Any],
    *,
    fallbacks: Optional[Iterable[Optional[str]]] = None,
) -> Dict[str, Any]:
    """Populate ``metadata['source']`` and ``metadata['source_file']``.

    Resolution order — first non-empty value wins:

      1. ``metadata['source']`` (already set by upstream code)
      2. ``metadata['source_file']`` (often set from frontmatter)
      3. ``metadata['original_source_file']`` (set when ingesting via API)
      4. ``metadata['md_file_name']`` (DB column carried in metadata)
      5. ``metadata['file_path']`` (some templates only have this)
      6. Each value from ``fallbacks`` in order (e.g. a column from the row)

    The resolved value is stored in BOTH ``source`` and ``source_file`` as
    its basename, since the evaluator's matcher checks both keys and golden
    ground truth uses bare filenames (``flow2.md`` rather than
    ``/some/path/flow2.md``).

    The function mutates and returns ``metadata`` in place. A non-dict input
    is replaced with a fresh dict.
    """
    if not isinstance(metadata, dict):
        metadata = {}

    candidates = [
        metadata.get("source"),
        metadata.get("source_file"),
        metadata.get("original_source_file"),
        metadata.get("md_file_name"),
        metadata.get("file_path"),
    ]
    if fallbacks:
        candidates.extend(fallbacks)

    name = ""
    for c in candidates:
        n = basename_or_empty(c)
        if n:
            name = n
            break

    if name:
        metadata["source"] = name
        metadata.setdefault("source_file", name)
    return metadata
