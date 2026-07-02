"""
Dbank path — the validated pipeline for Hebrew banking-regulation PDFs.

Encodes, per page, the routing we benchmarked on 368_8.pdf and validated on the
critical 224-page Bank Hapoalim report (duah_hapoalim.pdf):

1. PREFILTER (PyMuPDF char-ratio): keep Hebrew-dominant pages, drop the
   English-appendix noise. Dropped pages are logged (never silent).
2. TABLE DETECTION via docling's layout model. A single cheap docling pass
   (``do_table_structure=False``, ~0.7s/page on 1-CPU) already reports table
   regions in ``document.tables`` — TableFormer (2–4s/page) is NOT needed just
   to detect. This retires the unreliable fitz ``find_tables`` (found 2/217 on
   the finance doc) and the crude digit-ratio heuristic.
3. TABLE / IMAGE / SCANNED pages -> VLM. docling reverses Hebrew word order
   inside table cells and can't read figures, so these go to the VLM, which
   keeps correct RTL structure. VLM calls are network-bound, so they are run
   CONCURRENTLY (see DBANK_VLM_CONCURRENCY) — the difference between ~16 min
   serial and ~2 min on this doc.
4. PROSE pages -> docling (complete + correct Hebrew reading order), with a
   COMPLETENESS GUARD: if docling captured < ``completeness_min`` of the page's
   text-layer Hebrew words, fall back to the VLM. This is the check that caught
   kreuzberg silently dropping ~60% of text on most pages.

The docling converter and one VLM client are built once per conversion and
reused across pages.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import fitz

from app.registry import register_converter
from .base import PDFConverter

logger = logging.getLogger(__name__)

_HEB = re.compile(r"[֐-׿]+")
_MIN_HEB_RATIO = 0.5      # prefilter: keep pages at least this Hebrew
_COMPLETENESS_MIN = 0.85  # docling must capture >= this fraction of text-layer words
_SCANNED_MAX_CHARS = 50   # avg text-layer chars/page below this => treat doc as scanned
_IMAGE_MIN_AREA_FRAC = 0.10  # a page image covering >= this fraction routes to VLM
_VLM_CONCURRENCY = int(os.getenv("DBANK_VLM_CONCURRENCY", "8"))  # parallel VLM page calls
_PAGE_SEP = "\n\n---\n\n"


def _heb_word_count(text: str) -> int:
    return sum(1 for t in _HEB.findall(text) if len(t) >= 2)


def _heb_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(0x590 <= ord(c) <= 0x5FF for c in letters) / len(letters)


@register_converter(
    name="dbank",
    label="Dbank (Hebrew pipeline)",
    description=(
        "Hebrew banking-doc pipeline: PyMuPDF prefilter (drop non-Hebrew pages) -> "
        "docling for prose + docling-detected table/image pages to a parallel VLM "
        "(gemini), with a per-page completeness guard that falls back to the VLM "
        "when docling drops text."
    ),
)
class DbankConverter(PDFConverter):
    def __init__(
        self,
        prefilter: bool = True,
        min_heb_ratio: float = _MIN_HEB_RATIO,
        completeness_min: float = _COMPLETENESS_MIN,
        scanned_max_chars: int = _SCANNED_MAX_CHARS,
        vlm_concurrency: int = _VLM_CONCURRENCY,
    ) -> None:
        self._prefilter = prefilter
        self._min_heb = min_heb_ratio
        self._completeness_min = completeness_min
        self._scanned_max_chars = scanned_max_chars
        self._vlm_concurrency = max(1, vlm_concurrency)
        self._docling = None
        self._vlm = None

    # -- lazily-built, reused engines --------------------------------------

    def _docling_convert(self, pdf_path: str) -> tuple[str, int]:
        """Convert one page and report how many table regions docling detected.

        Returns ``(markdown, n_tables)``. do_table_structure stays False: the
        layout model still populates ``document.tables`` (detection), and we send
        table pages to the VLM anyway, so paying for TableFormer cell parsing
        would be wasted 2–4s/page.
        """
        if self._docling is None:
            from docling.document_converter import DocumentConverter, PdfFormatOption
            from docling.datamodel.pipeline_options import PdfPipelineOptions
            from docling.datamodel.base_models import InputFormat

            opts = PdfPipelineOptions()
            opts.do_ocr = False
            opts.do_table_structure = False
            # On CPU-constrained deployments (e.g. 1-vCPU Docker), pin docling's
            # inference threads so torch doesn't oversubscribe the single core.
            # Set DOCLING_CPU_THREADS=1 there; unset = docling's default (auto).
            threads = os.getenv("DOCLING_CPU_THREADS")
            if threads:
                from docling.datamodel.pipeline_options import (
                    AcceleratorOptions, AcceleratorDevice,
                )
                opts.accelerator_options = AcceleratorOptions(
                    num_threads=int(threads), device=AcceleratorDevice.CPU,
                )
            self._docling = DocumentConverter(
                format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
            )
        doc = self._docling.convert(pdf_path).document
        return doc.export_to_markdown(), len(getattr(doc, "tables", []) or [])

    def _ensure_vlm(self) -> None:
        if self._vlm is None:
            from .vlm import VLMConverter
            # temperature=0.0: table pages carry financial numbers, so decode
            # deterministically — no sampling, minimal risk of a hallucinated digit.
            self._vlm = VLMConverter(backend="factory", temperature=0.0)  # model via VLM_MODEL env

    @staticmethod
    def _render(page) -> str:
        from .vlm import VLMConverter
        return VLMConverter._render_page_as_b64(page)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _has_significant_image(page) -> bool:
        """True if the page carries a real figure/diagram (docling won't read it,
        the VLM can). Ignores tiny header/footer logos via an area threshold."""
        try:
            page_area = abs(page.rect.width * page.rect.height)
            if page_area <= 0:
                return False
            for info in page.get_image_info():
                bbox = info.get("bbox")
                if not bbox:
                    continue
                x0, y0, x1, y1 = bbox
                if abs((x1 - x0) * (y1 - y0)) / page_area >= _IMAGE_MIN_AREA_FRAC:
                    return True
        except Exception:
            return False
        return False

    @staticmethod
    def _write_page(doc, index: int) -> Path:
        sub = fitz.open()
        sub.insert_pdf(doc, from_page=index, to_page=index)
        fd, name = tempfile.mkstemp(suffix=f"_dbank_p{index}.pdf")
        os.close(fd)
        sub.save(name)
        sub.close()
        return Path(name)

    # -- PDFConverter interface --------------------------------------------

    def convert(self, pdf_path: Path) -> str:
        self.validate_path(pdf_path)

        # Each kept page becomes ["md", text] (final) or ["vlm", img_b64] (pending
        # parallel transcription). Order is preserved for the final join.
        slots: list[list] = []
        dropped: list[int] = []
        n_docling = 0

        with fitz.open(str(pdf_path)) as doc:
            total = doc.page_count

            # Scanned-doc guard: the prefilter and completeness guard both read the
            # text layer, so a scanned PDF (no text layer) would score heb_ratio 0
            # on every page and be dropped entirely. Detect a near-empty text layer
            # up-front and route every page to the VLM (which reads the rendered image).
            avg_text = (sum(len(doc[i].get_text().strip()) for i in range(total)) / total
                        ) if total else 0.0
            scanned = avg_text < self._scanned_max_chars
            if scanned:
                logger.warning(
                    "dbank %s: near-empty text layer (avg %.0f chars/page) — treating as "
                    "scanned, routing all %d pages to VLM", pdf_path.name, avg_text, total,
                )

            # Phase 1 (serial, CPU): prefilter, docling detection + prose, render
            # VLM-bound pages. fitz/docling run on the main thread only.
            for i in range(total):
                page = doc[i]
                text = page.get_text()

                if not scanned and self._prefilter and _heb_ratio(text) < self._min_heb:
                    dropped.append(i)
                    continue

                if scanned or self._has_significant_image(page):
                    slots.append(["vlm", self._render(page)])
                    continue

                tmp = self._write_page(doc, i)
                try:
                    md, n_tables = self._docling_convert(str(tmp))
                finally:
                    tmp.unlink(missing_ok=True)

                if n_tables > 0:
                    slots.append(["vlm", self._render(page)])
                    continue

                ref_words = _heb_word_count(text)
                if ref_words and _heb_word_count(md) < self._completeness_min * ref_words:
                    logger.warning(
                        "dbank: docling dropped text on page %d (%d/%d Hebrew words) "
                        "— VLM fallback", i, _heb_word_count(md), ref_words,
                    )
                    slots.append(["vlm", self._render(page)])
                else:
                    slots.append(["md", md])
                    n_docling += 1

            # Phase 2 (parallel, network): transcribe every VLM-bound page at once.
            vlm_slots = [k for k, s in enumerate(slots) if s[0] == "vlm"]
            if vlm_slots:
                self._ensure_vlm()
                workers = min(self._vlm_concurrency, len(vlm_slots))
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    futs = {ex.submit(self._vlm.transcribe_page, slots[k][1]): k
                            for k in vlm_slots}
                    for fut in as_completed(futs):
                        k = futs[fut]
                        slots[k] = ["md", fut.result()]

        logger.info(
            "dbank %s: %d pages -> %d docling, %d vlm (x%d), %d dropped(non-Hebrew)%s",
            pdf_path.name, total, n_docling, len(vlm_slots), self._vlm_concurrency,
            len(dropped), f" {dropped[:30]}" if dropped else "",
        )
        return _PAGE_SEP.join(s[1] for s in slots)
