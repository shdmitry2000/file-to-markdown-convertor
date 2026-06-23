"""
Dbank path — the validated pipeline for Hebrew banking-regulation PDFs.

Encodes, per page, the routing we benchmarked on 368_8.pdf:

1. PREFILTER (PyMuPDF char-ratio): keep Hebrew-dominant pages, drop the
   English-appendix noise. Dropped pages are logged (never silent).
2. TABLE pages -> VLM (gemini-2.5-pro). docling reverses Hebrew word order
   inside table cells, so tables go to the VLM.
3. PROSE pages -> docling (complete + correct Hebrew reading order).
4. COMPLETENESS GUARD: compare docling's Hebrew word count for the page against
   the PyMuPDF text-layer count; if docling captured < `completeness_min` of it,
   fall back to the VLM for that page. This is the check that caught kreuzberg
   silently dropping ~60% of text on most pages.

Engines are built once per conversion and reused across pages (docling stays
warm; the VLM reuses one client).
"""
from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path

import fitz

from app.registry import register_converter
from .base import PDFConverter

logger = logging.getLogger(__name__)

_HEB = re.compile(r"[֐-׿]+")
_MIN_HEB_RATIO = 0.5      # prefilter: keep pages at least this Hebrew
_COMPLETENESS_MIN = 0.85  # docling must capture >= this fraction of text-layer words
_TABLE_MIN_CELLS = 6      # a detected table this size routes the page to the VLM
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
        "docling for prose + VLM (gemini-2.5-pro) for tables, with a per-page "
        "completeness guard that falls back to the VLM when docling drops text."
    ),
)
class DbankConverter(PDFConverter):
    def __init__(
        self,
        prefilter: bool = True,
        min_heb_ratio: float = _MIN_HEB_RATIO,
        completeness_min: float = _COMPLETENESS_MIN,
    ) -> None:
        self._prefilter = prefilter
        self._min_heb = min_heb_ratio
        self._completeness_min = completeness_min
        self._docling = None
        self._vlm = None

    # -- lazily-built, reused engines --------------------------------------

    def _docling_convert(self, pdf_path: str) -> str:
        if self._docling is None:
            from docling.document_converter import DocumentConverter, PdfFormatOption
            from docling.datamodel.pipeline_options import PdfPipelineOptions
            from docling.datamodel.base_models import InputFormat

            opts = PdfPipelineOptions()
            opts.do_ocr = False
            opts.do_table_structure = True
            self._docling = DocumentConverter(
                format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
            )
        return self._docling.convert(pdf_path).document.export_to_markdown()

    def _vlm_convert(self, pdf_path: Path) -> str:
        if self._vlm is None:
            from .vlm import VLMConverter
            self._vlm = VLMConverter(backend="factory")  # defaults to gemini-2.5-pro
        return self._vlm.convert(pdf_path)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _has_table(page) -> bool:
        try:
            tables = page.find_tables().tables
        except Exception:
            return False
        return any(t.row_count * t.col_count >= _TABLE_MIN_CELLS for t in tables)

    @staticmethod
    def _write_page(doc, index: int) -> Path:
        sub = fitz.open()
        sub.insert_pdf(doc, from_page=index, to_page=index)
        fd, name = tempfile.mkstemp(suffix=f"_dbank_p{index}.pdf")
        import os
        os.close(fd)
        sub.save(name)
        sub.close()
        return Path(name)

    # -- PDFConverter interface --------------------------------------------

    def convert(self, pdf_path: Path) -> str:
        self.validate_path(pdf_path)

        pages_md: list[str] = []
        dropped: list[int] = []
        n_docling = n_vlm = 0

        with fitz.open(str(pdf_path)) as doc:
            total = doc.page_count
            for i in range(total):
                page = doc[i]
                text = page.get_text()

                if self._prefilter and _heb_ratio(text) < self._min_heb:
                    dropped.append(i)
                    continue

                ref_words = _heb_word_count(text)
                tmp = self._write_page(doc, i)
                try:
                    if self._has_table(page):
                        md = self._vlm_convert(tmp)
                        n_vlm += 1
                    else:
                        md = self._docling_convert(str(tmp))
                        if ref_words and _heb_word_count(md) < self._completeness_min * ref_words:
                            logger.warning(
                                "dbank: docling dropped text on page %d (%d/%d Hebrew words) "
                                "— VLM fallback", i, _heb_word_count(md), ref_words,
                            )
                            md = self._vlm_convert(tmp)
                            n_vlm += 1
                        else:
                            n_docling += 1
                finally:
                    tmp.unlink(missing_ok=True)

                pages_md.append(md)

        logger.info(
            "dbank %s: %d pages -> %d docling, %d vlm, %d dropped(non-Hebrew)%s",
            pdf_path.name, total, n_docling, n_vlm, len(dropped),
            f" {dropped[:30]}" if dropped else "",
        )
        return _PAGE_SEP.join(pages_md)
