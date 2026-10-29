"""
Dbank path — the validated pipeline for Hebrew banking-regulation PDFs.

Encodes, per page, the routing we benchmarked on 368_8.pdf and validated on the
critical 224-page Bank Hapoalim report (duah_hapoalim.pdf):

1. EVERY PAGE WITH TEXT IS KEPT, whatever its language. Only pages with an empty
   text layer and no figure are skipped (logged). A page whose text layer is
   unreadable (broken font encoding: mostly control characters) goes to the VLM.
   There used to be a Hebrew-ratio prefilter here; it emptied English PDFs and
   dropped Hebrew pages heavy with English terms (46-48% Hebrew).
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
   A PRECISION GUARD covers the opposite failure: on pages mixing Hebrew and
   Latin script docling can scramble words (reversed Latin words such as
   "edocinU", Hebrew split apart at its vowel marks). If fewer than
   ``precision_min`` of docling's words, per script, exist on the page's text
   layer, the page goes to the VLM.
5. NUMBER CHECK on VLM pages that have a text layer: every number >= 1000 the
   VLM writes must be on the page (see number_check.py). A page that fails is
   transcribed once more — by VLM_ESCALATION_MODEL (gemini-3.8-flash) on the same
   lane when the pod runs a weaker model; if the better attempt still fails it is kept, logged,
   and tagged with an HTML comment listing the numbers, so the flag travels with
   the page into chunks. The same pass runs the TABLE CHECK (table_check.py):
   each number in a markdown table must sit in its row and column on the page,
   which catches a right number in the wrong cell — invisible to the number check.
6. EMPTY VLM ANSWER: Vertex can block a harmless page outright (finish reason
   content_filter, promptFeedback SAFETY, not switchable via safety_settings).
   The page is asked once more; if it is still empty, a page docling already
   converted keeps docling's text rather than vanishing.

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

import pymupdf as fitz  # `fitz` is the deprecated alias for the same module

from app.registry import register_converter
from .base import PDFConverter
from .number_check import not_on_page, numbers, numeric_runs
from .table_check import misplaced

logger = logging.getLogger(__name__)

_HEB = re.compile(r"[֐-׿]+")
_COMPLETENESS_MIN = 0.85  # docling must capture >= this fraction of text-layer words
# Of docling's words (per script), at least this fraction must exist on the text layer.
# Measured on 73 sample pages: scrambled pages 0.00 and 0.77, every clean page >= 0.94.
_PRECISION_MIN = 0.85
_PRECISION_MIN_WORDS = 10  # a script with fewer words in docling's output is not judged
_LAT_WORD = re.compile(r"[A-Za-z]{3,}")
_DOCLING_PLACEHOLDER = re.compile(r"<!--.*?-->", re.S)  # e.g. <!-- formula-not-decoded -->
_SCANNED_MAX_CHARS = 50   # avg text-layer chars/page below this => treat doc as scanned
_IMAGE_MIN_AREA_FRAC = 0.10  # a page image covering >= this fraction routes to VLM
# Text layers from a broken font encoding are mostly C0 control characters (measured
# 0.82-0.95 on such pages, <= 0.04 on every readable page of the sample set).
_UNREADABLE_CTRL_RATIO = 0.20
_VLM_CONCURRENCY = int(os.getenv("DBANK_VLM_CONCURRENCY", "8"))  # parallel VLM page calls
# A page that fails the number or table check is asked again of this model, on the
# same lane. A weaker model asked twice is a coin flip: gemini-3.5-flash-lite put a
# year header over the wrong column on retry as often as not, while gemini-3.8-flash
# had no misplaced cell on the 20-page table bench. Empty = retry the same model.
_ESCALATION_MODEL = os.getenv("VLM_ESCALATION_MODEL", "gemini-3.8-flash").strip()
_PAGE_SEP = "\n\n---\n\n"
# Each kept page starts with its ORIGINAL 1-based page number. Skipped (blank)
# pages leave gaps, so counting separators cannot recover page numbers; templates use
# the marker for citations and to align the text with an outline built from the PDF.
_PAGE_MARK = "<!-- page {} -->"


def _heb_word_count(text: str) -> int:
    return sum(1 for t in _HEB.findall(text) if len(t) >= 2)


def _word_precision(md: str, text: str) -> float:
    """Lowest share, over Hebrew and Latin, of docling's words found on the text layer.

    Scrambled output (reversed or split words) is made of words the page does not
    contain. Docling's own placeholder comments are not page words, so they are
    removed first.
    """
    md = _DOCLING_PLACEHOLDER.sub(" ", md)
    worst = 1.0
    for words in (
        lambda t: [w for w in _HEB.findall(t) if len(w) >= 2],
        _LAT_WORD.findall,
    ):
        out = words(md)
        if len(out) >= _PRECISION_MIN_WORDS:
            ref = set(words(text))
            worst = min(worst, sum(w in ref for w in out) / len(out))
    return worst


def _control_ratio(text: str) -> float:
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    return sum(ord(c) < 32 for c in chars) / len(chars)


def _heb_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(0x590 <= ord(c) <= 0x5FF for c in letters) / len(letters)


@register_converter(
    name="dbank",
    label="Dbank (Hebrew pipeline)",
    description=(
        "Hebrew banking-doc pipeline: "
        "docling for prose + docling-detected table/image pages to a parallel VLM "
        "(gemini), with a per-page completeness guard that falls back to the VLM "
        "when docling drops text."
    ),
)
class DbankConverter(PDFConverter):
    def __init__(
        self,
        completeness_min: float = _COMPLETENESS_MIN,
        precision_min: float = _PRECISION_MIN,
        scanned_max_chars: int = _SCANNED_MAX_CHARS,
        vlm_concurrency: int = _VLM_CONCURRENCY,
    ) -> None:
        self._completeness_min = completeness_min
        self._precision_min = precision_min
        self._scanned_max_chars = scanned_max_chars
        self._vlm_concurrency = max(1, vlm_concurrency)
        self._docling = None
        self._vlm = None
        self._strong = None  # the escalation model, when it differs from the VLM's

    # -- lazily-built, reused engines --------------------------------------

    def _docling_convert(self, pdf_path: str, page: int | None = None) -> tuple[str, int]:
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
        from .image_ids import annotate_images
        # One page at a time: docling sees page 1, the caller knows the real one.
        return annotate_images(doc.export_to_markdown(), doc, page=page), len(getattr(doc, "tables", []) or [])

    def _ensure_vlm(self) -> None:
        if self._vlm is None:
            from .vlm import VLMConverter
            # Temperature from VLM_TEMPERATURE (unset = model default). It was pinned
            # to 0.0 for the financial numbers; on Gemini 3 that only drew a warning
            # per call: an A/B on Hebrew bank-report table pages found the model
            # default as exact on digits (recall 0.998-1.0, no invented numbers)
            # and faster.
            self._vlm = VLMConverter(backend="factory")  # the job's lane (vlm_lane)
            if _ESCALATION_MODEL:
                # Same lane, stronger model. The lane differs per job (space or pod), so
                # whether it is really another model is only known at call time.
                self._strong = VLMConverter(backend="factory", model_name=_ESCALATION_MODEL)

    def _escalates(self) -> bool:
        """Whether a retry goes to the escalation model: not when this job's lane (the
        space's or the pod's) already runs it. An unknown lane escalates as configured."""
        if self._strong is None:
            return False
        from . import vlm_lane
        try:
            return vlm_lane.model() != vlm_lane.model(_ESCALATION_MODEL)
        except Exception:  # noqa: BLE001 - no lane configured; the call itself will say
            return True

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

    def _transcribe_checked(self, img_b64: str, runs, page_index: int) -> str:
        """Transcribe a page; if it has numbers the page does not, or numbers in the
        wrong table cell, try once more. ``runs``: the page's numeric_runs."""
        md = self._vlm.transcribe_page(img_b64)
        if not md.strip():
            md = self._vlm.transcribe_page(img_b64)  # a provider block is intermittent
        if runs is None:
            return md
        ref = numbers("\n".join(text for text, _x, _y in runs))

        def check(text):
            return not_on_page(text, ref), misplaced(text, runs)[1]

        bad, cells = check(md)
        if not bad and not cells:
            return md
        strong = self._strong if self._escalates() else None
        retry = (strong or self._vlm).transcribe_page(img_b64)
        retry_bad, retry_cells = check(retry)
        if len(retry_bad) + len(retry_cells) < len(bad) + len(cells):
            md, bad, cells = retry, retry_bad, retry_cells
        if not bad and not cells:
            logger.info("dbank: page %d number/table check passed on retry%s", page_index,
                        f" ({_ESCALATION_MODEL})" if strong else "")
            return md
        notes = []
        if bad:
            shown = ", ".join(f"{v:,}" for v in bad[:20])
            logger.warning("dbank: page %d has numbers not on the page after retry: %s",
                           page_index, shown)
            notes.append(f"<!-- dbank: numbers not on the page: {shown} -->")
        if cells:
            shown = "; ".join(cells[:20])
            logger.warning("dbank: page %d has table cells off their place after retry: %s",
                           page_index, shown)
            notes.append(f"<!-- dbank: table cells not where the page has them: {shown} -->")
        return md + "\n\n" + "\n".join(notes)

    # -- PDFConverter interface --------------------------------------------

    def convert(self, pdf_path: Path) -> str:
        self.validate_path(pdf_path)

        # Each kept page becomes ["md", text, page] (final) or
        # ["vlm", img_b64, ref, page, docling_md] (docling_md: kept if the VLM answers
        # empty; None for pages docling never saw)
        # (pending parallel transcription; ref = the page's numbers to check the
        # transcription against, None when there is nothing to check). Order is
        # preserved for the final join.
        slots: list[list] = []
        blank: list[int] = []
        n_docling = 0

        with fitz.open(str(pdf_path)) as doc:
            total = doc.page_count

            # Scanned-doc guard: routing and the completeness guard both read the
            # text layer, so a scanned PDF (no text layer) would have every page
            # skipped as blank. Detect a near-empty text layer
            # up-front and route every page to the VLM (which reads the rendered image).
            avg_text = (sum(len(doc[i].get_text().strip()) for i in range(total)) / total
                        ) if total else 0.0
            scanned = avg_text < self._scanned_max_chars
            if scanned:
                logger.warning(
                    "dbank %s: near-empty text layer (avg %.0f chars/page) — treating as "
                    "scanned, routing all %d pages to VLM", pdf_path.name, avg_text, total,
                )

            # Phase 1 (serial, CPU): docling detection + prose, render VLM-bound
            # pages. fitz/docling run on the main thread only.
            for i in range(total):
                page = doc[i]
                text = page.get_text()

                if (scanned or self._has_significant_image(page)
                        or _control_ratio(text) >= _UNREADABLE_CTRL_RATIO):
                    # No check: a scanned page has no text layer, figures in an
                    # image page carry numbers the text layer does not have, and an
                    # unreadable text layer has no trustworthy numbers.
                    slots.append(["vlm", self._render(page), None, i, None])
                    continue

                if not text.strip():
                    blank.append(i)
                    continue

                tmp = self._write_page(doc, i)
                try:
                    md, n_tables = self._docling_convert(str(tmp), page=i + 1)
                finally:
                    tmp.unlink(missing_ok=True)

                if n_tables > 0:
                    slots.append(["vlm", self._render(page), numeric_runs(page), i, md])
                    continue

                ref_words = _heb_word_count(text)
                if ref_words and _heb_word_count(md) < self._completeness_min * ref_words:
                    logger.warning(
                        "dbank: docling dropped text on page %d (%d/%d Hebrew words) "
                        "— VLM fallback", i, _heb_word_count(md), ref_words,
                    )
                    slots.append(["vlm", self._render(page), numeric_runs(page), i, md])
                elif (precision := _word_precision(md, text)) < self._precision_min:
                    logger.warning(
                        "dbank: docling scrambled words on page %d (%.0f%% found on the "
                        "page) — VLM fallback", i, precision * 100,
                    )
                    slots.append(["vlm", self._render(page), numeric_runs(page), i, md])
                else:
                    slots.append(["md", md, i])
                    n_docling += 1

            # Phase 2 (parallel, network): transcribe every VLM-bound page at once.
            vlm_slots = [k for k, s in enumerate(slots) if s[0] == "vlm"]
            if vlm_slots:
                self._ensure_vlm()
                workers = min(self._vlm_concurrency, len(vlm_slots))
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    futs = {ex.submit(self._transcribe_checked, *slots[k][1:4]): k
                            for k in vlm_slots}
                    for fut in as_completed(futs):
                        k = futs[fut]
                        md, page_i, docling_md = fut.result(), slots[k][3], slots[k][4]
                        if not md.strip():
                            logger.warning(
                                "dbank: VLM returned nothing for page %d — %s", page_i,
                                "keeping docling's text" if docling_md else "page left empty",
                            )
                            md = docling_md or ""
                        slots[k] = ["md", md, page_i]

        logger.info(
            "dbank %s: %d pages -> %d docling, %d vlm (x%d), %d blank skipped%s",
            pdf_path.name, total, n_docling, len(vlm_slots), self._vlm_concurrency,
            len(blank), f" {blank[:30]}" if blank else "",
        )
        return _PAGE_SEP.join(f"{_PAGE_MARK.format(s[2] + 1)}\n\n{s[1]}" for s in slots)
