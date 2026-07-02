"""
PDF-to-Markdown converter using any OpenAI-compatible Vision-Language Model.

Defaults to Ollama running locally (qwen3-vl), but can be pointed at
any provider that exposes an OpenAI-compatible chat completions endpoint.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import time
from pathlib import Path
from typing import Callable, Optional

import httpx

from app.registry import register_converter
from .base import PDFConverter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_PROMPT = """Convert the PDF page image to clean markdown.

**Text:** Preserve exact content, reading order, and hierarchy (#, ##, ###). Use standard markdown for bold, italic, code, lists, blockquotes, footnotes.
**Tables:** Full markdown table syntax with alignment.
**Math:** LaTeX inline $...$ or display $$...$$.
**Visuals:** Replace with `![<type>: <description of content, labels, trends>](image)`.
**Code:** Triple backticks with language tag.

Output raw markdown only. No preamble, no commentary, no wrapping code block."""

# DPI used when rasterising each PDF page before sending it to the VLM.
_RENDER_DPI = 300
_DPI_SCALE = _RENDER_DPI / 72  # fitz uses 72 DPI as its baseline

_DEFAULT_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")

# Hard cap on tokens generated per page. A single page of markdown is well under
# this; the cap exists to stop the degenerate repetition loops some VLMs fall
# into on dense tables (observed: 1M+ chars of garbage on a Hebrew/English table).
_DEFAULT_MAX_TOKENS = int(os.getenv("VLM_MAX_TOKENS", "32000"))

# Page transcription is a mechanical OCR-style task, not a reasoning task, so
# thinking is disabled by default — it only adds latency (dense financial tables
# were ~55s/page with thinking on). Empty string = leave the model default.
# On gemini-3.x "disable" maps to the lowest thinking level (thinkingLevel=low,
# no thought output); on 2.5-class models it drops the thinking budget to 0.
_DEFAULT_REASONING_EFFORT = os.getenv("VLM_REASONING_EFFORT", "disable")

# Retry settings for timed-out VLM calls.
_MAX_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY_S = 1.0  # seconds; doubles each attempt (1 s, 2 s, 4 s)


@register_converter(
    name="vlm",
    label="VLM (Vision-Language Model)",
    description=(
        "Uses Vision-Language Model for full page-by-page image analysis. "
        "Best for scanned PDFs, images, and complex visual content. Requires VLM endpoint."
    ),
)
class VLMConverter(PDFConverter):
    """PDF-to-Markdown converter using any OpenAI-compatible VLM.

    Each page is rasterised at :data:`_RENDER_DPI` DPI and sent to the model
    as a base64-encoded PNG embedded in a ``data:`` URI.

    The ``http_client`` parameter accepts a shared ``httpx.Client`` so all
    conversions reuse the same connection pool instead of creating a new one
    per request.  Pass ``app.state.http_client_sync`` from the router.

    Provider examples::

        # Ollama (default) — no API key required
        converter = VLMConverter(http_client=shared_client)

        # OpenAI
        converter = VLMConverter(
            model="gpt-4o",
            base_url="https://api.openai.com/v1",
            api_key="sk-...",
            http_client=shared_client,
        )
    """

    def __init__(
        self,
        model: str = "qwen3-vl:4b-instruct-q4_K_M",
        base_url: str = _DEFAULT_BASE_URL,
        api_key: str = "ollama",
        temperature: float = 0.0,
        user_prompt: Optional[str] = None,
        on_progress: Optional[Callable[[int, int], None]] = None,
        http_client: Optional[httpx.Client] = None,
        backend: Optional[str] = None,
        max_tokens: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
    ) -> None:
        self._temperature = temperature
        self._max_tokens = max_tokens if max_tokens is not None else _DEFAULT_MAX_TOKENS
        # None => use the env-derived default ("disable"); "" => leave model default.
        self._reasoning_effort = (
            reasoning_effort if reasoning_effort is not None else _DEFAULT_REASONING_EFFORT
        )
        self._user_prompt = user_prompt
        self._on_progress = on_progress
        # backend: "openai" (default, OpenAI-compatible/Ollama) or "factory"
        # (route vision through the shared LLMFactory -> litellm -> Gemini/Vertex,
        # using the same provider + credentials the rest of the platform uses).
        self._backend = (backend or os.getenv("VLM_BACKEND", "openai")).lower()

        if self._backend == "factory":
            from shared.llm_factory import LLMFactory

            # Use the factory as the single source of truth for model + region +
            # provider auth. Model comes from LLM_FACTORY_PROVIDER (e.g.
            # vertex_ai/gemini-3.5-flash); set VLM_MODEL only to give the vision
            # lane a *different* model than the general LLM lane. The factory
            # client owns vertex_location (gemini-3.x is global-only) and does the
            # vision call via vision_sync — no model/region config duplicated here.
            model_override = os.getenv("VLM_MODEL")
            self._client = (LLMFactory.build(model_override) if model_override
                            else LLMFactory.from_env())
            if not hasattr(self._client, "vision_sync"):
                raise RuntimeError(
                    "VLM factory backend needs a litellm/vertex provider with vision "
                    f"(got {type(self._client).__name__}); set LLM_FACTORY_PROVIDER / "
                    "VLM_MODEL to a vertex_ai or gemini model."
                )
            self._model = getattr(self._client, "_model", "factory")
            self._retry_exc: tuple = (Exception,)
            logger.info("VLMConverter backend=factory model=%s", self._model)
            return

        from openai import OpenAI, APITimeoutError

        self._model = model
        self._retry_exc = (APITimeoutError,)
        # If a shared client is supplied its timeout settings apply.
        # Otherwise use a conservative per-request timeout.
        client_kwargs: dict = dict(base_url=base_url, api_key=api_key)
        if http_client is not None:
            client_kwargs["http_client"] = http_client
        else:
            client_kwargs["timeout"] = httpx.Timeout(
                connect=10.0, read=120.0, write=10.0, pool=5.0
            )

        self._client = OpenAI(**client_kwargs)

    # ------------------------------------------------------------------
    # PDFConverter interface
    # ------------------------------------------------------------------

    def convert(self, pdf_path: Path) -> str:
        """Render every page and send each one to the VLM for transcription.

        Cancellation is signalled via the ``on_progress`` callback passed at
        construction time (see :meth:`document_service._progress_handler`):
        it raises ``InterruptedError`` when the caller's stop_event is set,
        which bubbles out of this method naturally.

        Returns:
            Full document as Markdown, pages separated by ``\\n\\n---\\n\\n``.
        """
        import fitz  # PyMuPDF

        self.validate_path(pdf_path)

        pages: list[str] = []

        with fitz.open(str(pdf_path)) as pdf_document:
            total = pdf_document.page_count
            for page_num in range(total):
                page = pdf_document[page_num]
                img_b64 = self._render_page_as_b64(page)
                markdown = self._transcribe_page_with_retry(img_b64)
                pages.append(markdown)

                if self._on_progress:
                    # The progress handler checks stop_event and raises
                    # InterruptedError when a cancellation is requested.
                    self._on_progress(page_num + 1, total)

        return "\n\n---\n\n".join(pages)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _render_page_as_b64(page) -> str:
        """Rasterise a fitz page and return a base64-encoded PNG string."""
        matrix = __import__("fitz").Matrix(_DPI_SCALE, _DPI_SCALE)
        pix = page.get_pixmap(matrix=matrix)
        return base64.b64encode(pix.tobytes("png")).decode("utf-8")

    def _transcribe_page_factory(self, img_b64: str, prompt_text: str) -> str:
        """Transcribe one page through the shared LLMFactory client's vision_sync
        (the factory owns model + region + provider auth; the thinking-disable
        fallback for models that reject it lives there too)."""
        content = self._client.vision_sync(
            prompt_text, [img_b64],
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            reasoning_effort=self._reasoning_effort or None,
        ).strip()
        content = re.sub(r"^```(?:markdown)?\n?", "", content)
        content = re.sub(r"\n?```$", "", content)
        return content.strip()

    def _transcribe_page(self, img_b64: str) -> str:
        """Send a base64 page image to the VLM and return the Markdown text."""
        prompt_text = self._user_prompt if self._user_prompt else _PROMPT
        if self._backend == "factory":
            return self._transcribe_page_factory(img_b64, prompt_text)
        response = self._client.chat.completions.create(
            model=self._model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": f"data:image/png;base64,{img_b64}",
                        },
                        {"type": "text", "text": prompt_text},
                    ],
                },
            ],
        )
        content = (response.choices[0].message.content or "").strip()
        content = re.sub(r"^```(?:markdown)?\n?", "", content)
        content = re.sub(r"\n?```$", "", content)
        return content.strip()

    def transcribe_page(self, img_b64: str) -> str:
        """Public entry for a single pre-rendered page image (base64 PNG).

        Lets callers (e.g. the dbank pipeline) render pages themselves and fan
        transcription out across threads — VLM calls are network-bound, so a
        thread pool over this method parallelises cleanly while one converter
        instance (and its factory/client config) is shared read-only.
        """
        return self._transcribe_page_with_retry(img_b64)

    def _transcribe_page_with_retry(self, img_b64: str) -> str:
        """Call ``_transcribe_page`` with exponential back-off on timeout.

        On final timeout the exception is re-raised so the caller can decide
        whether to mark the page as failed or abort the entire conversion.
        """
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRY_ATTEMPTS):
            try:
                return self._transcribe_page(img_b64)
            except self._retry_exc as exc:
                last_exc = exc
                if attempt < _MAX_RETRY_ATTEMPTS - 1:
                    delay = _RETRY_BASE_DELAY_S * (2 ** attempt)
                    logger.warning(
                        "VLM page call timed out (attempt %d/%d), retrying in %.0fs",
                        attempt + 1,
                        _MAX_RETRY_ATTEMPTS,
                        delay,
                    )
                    time.sleep(delay)
                else:
                    logger.error(
                        "VLM page call timed out after %d attempts — aborting conversion",
                        _MAX_RETRY_ATTEMPTS,
                    )
        raise last_exc  # type: ignore[misc]
