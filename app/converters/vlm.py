"""
PDF-to-Markdown converter: each page image is transcribed by the platform's LLM.

The lane (type, model, identity) is the job's — the space's connector or this pod's
LLM_CONNECTOR_TYPE + LLM_TYPE (see vlm_lane). Temperature, max tokens, thinking and
the retry of a busy or failed call are the shared LLM layer's (shared/llm_factory:
LLM_TEMPERATURE, LLM_MAX_TOKENS — 32000 on the markdown pod — thinking off, the one
retry). This module reads no LLM setting itself.
"""

from __future__ import annotations

import base64
import logging
import re
from pathlib import Path
from typing import Callable, Optional

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


@register_converter(
    name="vlm",
    label="VLM (Vision-Language Model)",
    description=(
        "Uses Vision-Language Model for full page-by-page image analysis. "
        "Best for scanned PDFs, images, and complex visual content. Uses the platform LLM."
    ),
)
class VLMConverter(PDFConverter):
    """PDF-to-Markdown converter: each page is rasterised at :data:`_RENDER_DPI`
    DPI and transcribed by the job's LLM lane (``vision_sync``)."""

    def __init__(
        self,
        user_prompt: Optional[str] = None,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        self._user_prompt = user_prompt
        self._on_progress = on_progress
        # The lane is picked per job by vlm_lane and built on the first call, so a
        # converter that never calls the LLM never needs its credentials.

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
        self._build_lane()

        pages: list[str] = []

        with fitz.open(str(pdf_path)) as pdf_document:
            total = pdf_document.page_count
            for page_num in range(total):
                page = pdf_document[page_num]
                img_b64 = self._render_page_as_b64(page)
                markdown = self._transcribe_page(img_b64)
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

    def _transcribe_page(self, img_b64: str) -> str:
        """Transcribe one page through the job's client's vision_sync. The shared
        layer owns model, region, auth, generation settings and the retry; a job
        that brought its caller's connector goes through that instead (vlm_lane)."""
        from . import vlm_lane

        prompt_text = self._user_prompt if self._user_prompt else _PROMPT
        content = vlm_lane.client().vision_sync(prompt_text, [img_b64]).strip()
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
        self._build_lane()
        return self._transcribe_page(img_b64)

    def _build_lane(self) -> None:
        """Build this job's client before the first call (cached after it): a lane
        that cannot be built is a configuration error, and retrying it only delays
        the message that says which setting is missing."""
        from . import vlm_lane
        vlm_lane.client()
