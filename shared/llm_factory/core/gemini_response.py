"""Read a Gemini ``generateContent`` response the way the connector guide describes.

- The answer is every part of ``candidates[0].content.parts`` that has ``text`` and is
  not ``thought: true``, in order (a thought part may come first; the answer may span
  several parts).
- ``candidates[].finishReason`` says why generation stopped.
- A blocked or filtered prompt may carry ``promptFeedback`` instead of ``candidates``.
"""

from __future__ import annotations

from typing import Any


def _answer_parts(candidate: dict[str, Any]) -> list[str]:
    parts = (candidate.get("content") or {}).get("parts") or []
    return [p["text"] for p in parts
            if isinstance(p, dict) and p.get("text") and not p.get("thought")]


def answer_text(data: dict[str, Any]) -> str:
    """The answer text of a non-streaming response; ``ValueError`` naming the reason
    (``promptFeedback`` / ``finishReason``) when there is none."""
    candidates = data.get("candidates") if isinstance(data, dict) else None
    if not candidates:
        feedback = data.get("promptFeedback") if isinstance(data, dict) else None
        raise ValueError(f"Gemini returned no candidates (promptFeedback={feedback!r})")
    texts = _answer_parts(candidates[0])
    if not texts:
        raise ValueError(
            f"Gemini returned no answer text (finishReason={candidates[0].get('finishReason')!r})")
    return "".join(texts)


def stream_delta(chunk: dict[str, Any]) -> str:
    """The answer text in one streamed chunk ("" when it carries none)."""
    candidates = chunk.get("candidates") if isinstance(chunk, dict) else None
    return "".join(_answer_parts(candidates[0])) if candidates else ""
