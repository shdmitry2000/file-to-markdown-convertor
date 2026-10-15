"""
Slides lane — presentation decks saved as PDF (PowerPoint, Keynote, Google Slides).

Why docling is the wrong engine for these (measured 2026-10-15 on real Hebrew
decks): a map/diagram/photo slide has a title and a few words in its text
layer, so docling emits ``<!-- image -->`` and the content is gone, and no
quality guard fires because *some* text came out (a ~200 KB PowerPoint PDF
produced 4 chunks in production). Designed layouts (Google Slides "cards")
come out with the cards in reverse order and RTL punctuation flipped.

So the whole deck goes to the VLM, one call per slide, in parallel. Decks are
short, and the VLM is the only engine that both reads the figures and keeps
the right-to-left reading order on designed slides. The prompt asks for a full
transcription plus a description of every visual in the slide's own language,
so a retriever can match questions against the map/diagram content.

Detection (:func:`is_slide_deck`) looks at the INPUT, not at the output —
output-side checks cannot tell a deck that lost its pictures from a short
memo. Every rule requires landscape pages:

* ``metadata`` — creator/producer names a presentation tool.
* ``aspect``   — all pages share one size and the ratio is 16:9. 4:3 is too
  close to landscape Letter (1.29 vs 1.33) and needs the metadata rule.
* ``visual``   — most pages carry a large image and little text (stripped
  metadata, 4:3 decks, brochures, infographics).

Output contract matches dbank: every slide starts with ``<!-- page N -->`` and
slides are joined with ``---``, so citations and the header-aware chunkers
work unchanged (one slide ≈ one chunk).
"""
from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymupdf as fitz

from app.registry import register_converter
from .base import PDFConverter
from .number_check import not_on_page, page_numbers

logger = logging.getLogger(__name__)

_SLIDE_TOOLS = re.compile(r"powerpoint|keynote|google|impress|slides", re.I)
_ASPECT_16_9 = 16 / 9
_ASPECT_TOL = 0.01            # relative tolerance on the 16:9 rule
_VISUAL_MIN_IMAGE_FRAC = 0.40  # "visual" page: largest image covers >= this
_VISUAL_MAX_WORDS = 40         # ... and the text layer has fewer words than this
_IMAGE_MIN_AREA_FRAC = 0.10    # number check is skipped on slides with a figure (as dbank)
_VLM_CONCURRENCY = int(os.getenv("DBANK_VLM_CONCURRENCY", "8"))
_PAGE_SEP = "\n\n---\n\n"
_PAGE_MARK = "<!-- page {} -->"

_SLIDE_PROMPT = """This is one slide from a presentation. Convert it to markdown for a search index.

- Start with the slide title as a `##` heading.
- Transcribe ALL visible text exactly (Hebrew right-to-left text must be written as readable Hebrew words in the correct order). Keep bullets as markdown lists, tables as markdown tables.
- For every chart, diagram, map, photo or infographic, add a short paragraph in the SAME language as the slide describing what it shows: what entities/labels/numbers/trends it contains and the message it conveys.
- Do not invent numbers that are not on the slide.

Output raw markdown only, no preamble, no code fence."""


def _largest_image_frac(page) -> float:
    """Area of the largest image on the page as a fraction of the page area."""
    try:
        page_area = abs(page.rect.width * page.rect.height)
        if page_area <= 0:
            return 0.0
        best = 0.0
        for info in page.get_image_info():
            bbox = info.get("bbox")
            if bbox:
                x0, y0, x1, y1 = bbox
                best = max(best, abs((x1 - x0) * (y1 - y0)) / page_area)
        return best
    except Exception:
        return 0.0


def is_slide_deck(pdf_path: Path | str) -> str | None:
    """Return why *pdf_path* is a slide deck (``metadata`` | ``aspect`` |
    ``visual``), or ``None`` when it should stay on the document lane."""
    with fitz.open(str(pdf_path)) as doc:
        if doc.page_count == 0:
            return None
        first = doc[0].rect
        if first.width <= first.height:
            return None  # decks are landscape; portrait is a document

        meta = doc.metadata or {}
        if _SLIDE_TOOLS.search(f"{meta.get('creator', '')} {meta.get('producer', '')}"):
            return "metadata"

        sizes = {(round(p.rect.width), round(p.rect.height)) for p in doc}
        if len(sizes) == 1 and abs(first.width / first.height - _ASPECT_16_9) <= _ASPECT_TOL * _ASPECT_16_9:
            return "aspect"

        visual = sum(
            1 for p in doc
            if _largest_image_frac(p) >= _VISUAL_MIN_IMAGE_FRAC
            and len(p.get_text().split()) < _VISUAL_MAX_WORDS
        )
        if visual * 2 > doc.page_count:
            return "visual"
    return None


@register_converter(
    name="slides",
    label="Slides (VLM per slide)",
    description=(
        "Presentation decks saved as PDF: every slide is rendered and transcribed "
        "by the VLM in parallel, with a description of each map/chart/diagram in "
        "the slide's language. Auto-selected for detected decks on the docling lane."
    ),
)
class SlidesConverter(PDFConverter):
    def __init__(self, vlm_concurrency: int = _VLM_CONCURRENCY) -> None:
        self._vlm_concurrency = max(1, vlm_concurrency)
        self._vlm = None

    def _ensure_vlm(self) -> None:
        if self._vlm is None:
            from .vlm import VLMConverter
            self._vlm = VLMConverter(backend="factory", temperature=0.0, user_prompt=_SLIDE_PROMPT)

    @staticmethod
    def _render(page) -> str:
        from .vlm import VLMConverter
        return VLMConverter._render_page_as_b64(page)

    def _transcribe(self, img_b64: str, ref, page_index: int) -> str:
        """Transcribe one slide. Numbers the VLM writes that are not on the slide
        are tagged, never retried: on a slide with a figure the numbers live in
        the raster (map labels), so there is no reference to retry against."""
        md = self._vlm.transcribe_page(img_b64)
        if ref is None:
            return md
        bad = not_on_page(md, ref)
        if not bad:
            return md
        shown = ", ".join(f"{v:,}" for v in bad[:20])
        logger.warning("slides: page %d has numbers not on the slide: %s", page_index + 1, shown)
        return f"{md}\n\n<!-- slides: numbers not on the slide: {shown} -->"

    def convert(self, pdf_path: Path) -> str:
        self.validate_path(pdf_path)
        jobs: list[tuple[str, object, int]] = []  # (img_b64, number-check ref | None, index)
        with fitz.open(str(pdf_path)) as doc:
            for i in range(doc.page_count):
                page = doc[i]
                ref = None if _largest_image_frac(page) >= _IMAGE_MIN_AREA_FRAC else page_numbers(page)
                jobs.append((self._render(page), ref, i))

        self._ensure_vlm()
        with ThreadPoolExecutor(max_workers=min(self._vlm_concurrency, max(1, len(jobs)))) as ex:
            parts = list(ex.map(lambda j: self._transcribe(*j), jobs))

        logger.info("slides %s: %d slides -> vlm (x%d)", pdf_path.name, len(jobs), self._vlm_concurrency)
        return _PAGE_SEP.join(f"{_PAGE_MARK.format(i + 1)}\n\n{md}" for md, (_, _, i) in zip(parts, jobs))
