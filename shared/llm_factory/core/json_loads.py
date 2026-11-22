"""Tolerant JSON parsing for LLM responses.

Models (notably Gemini Flash Lite on Hebrew/RTL content) occasionally emit
structurally-broken JSON even with ``response_format=json_object`` — most often
an unescaped double-quote inside a string value, which trips the strict parser
mid-document with an "Expecting ',' delimiter" error. ``json_repair`` recovers
these without losing the embedded quotes.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class ReplyCutOffError(ValueError):
    """The reply stopped at the output-token cap and what arrived is not JSON.

    A ValueError like any unparseable reply, so callers that catch those are unchanged;
    a caller that can say "cut off" (golden drafting) catches this one.
    """


def loads_tolerant(content: str, *, model: str = "", source: str = "",
                   truncated: bool = False) -> Any:
    """Parse JSON, repairing common LLM malformations as a fallback.

    Tries the strict stdlib parser first; on failure, falls back to
    ``json_repair``. Raises ``ValueError`` only if both strict parsing and
    repair fail (or repair yields nothing usable) — ``ReplyCutOffError`` when
    ``truncated`` says the reply stopped at the output-token cap.
    """
    error = ReplyCutOffError if truncated else ValueError
    cut = " (the reply was cut off at the output-token limit)" if truncated else ""
    try:
        return json.loads(content)
    except json.JSONDecodeError as strict_err:
        try:
            import json_repair

            repaired = json_repair.loads(content)
        except Exception as repair_err:  # pragma: no cover - defensive
            raise error(
                f"{model or 'model'} did not return valid JSON{source}{cut} and repair "
                f"failed: {strict_err}; repair error: {repair_err}. "
                f"Content: {content[:300]!r}"
            ) from strict_err

        if repaired == "" or repaired is None:
            raise error(
                f"{model or 'model'} did not return valid JSON{source}{cut} and repair "
                f"produced no result: {strict_err}. Content: {content[:300]!r}"
            ) from strict_err

        logger.warning(
            "Repaired malformed JSON from %s%s (%s)",
            model or "model",
            source,
            strict_err,
        )
        return repaired
