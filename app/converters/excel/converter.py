"""Formula-aware Excel -> Markdown converter.

The default `.xlsx` route (markitdown) keeps the numbers but drops every formula,
so a totals cell arrives as a bare figure with nothing tying it to the rows that
produced it — unanswerable for questions like "what is this total made of".
Docling's Excel backend reads the same values-only view.

This converter reads the formulas openpyxl exposes, names each cell from metadata
the workbook already carries (comments, defined names, Table headers, freeze
panes, then header/label inference), and writes the relationships out alongside
the tables. No model is called and nothing is recalculated: a formula whose
result Excel never saved is reported as such rather than computed.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from app.registry import register_converter

from ..base import PDFConverter
from .reader import DEFAULT_MAX_CELLS, read_workbook
from .render_md import render_markdown

logger = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = {".xlsx", ".xlsm", ".xltx", ".xltm"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer — using %d", name, raw, default)
        return default


@register_converter(
    name="excel",
    label="Excel (formula-aware)",
    description=(
        "Spreadsheets with their formulas kept: one table per region, plus each "
        "formula expanded into labelled terms (Total=350 = Loan A=100 + Loan B=50) "
        "and a dependency list. Labels come from workbook metadata, not an LLM."
    ),
)
class ExcelConverter(PDFConverter):
    """Convert .xlsx/.xlsm to Markdown, preserving formula semantics.

    Legacy .xls is not handled: openpyxl cannot read it, and xlrd exposes no
    formulas — those files stay on the markitdown route.
    """

    def __init__(
        self,
        max_cells: int | None = None,
        max_refs: int | None = None,
        depth: int | None = None,
        emit_dependencies: bool | None = None,
    ) -> None:
        self._max_cells = max_cells or _env_int("EXCEL_MAX_CELLS", DEFAULT_MAX_CELLS)
        self._max_refs = max_refs or _env_int("EXCEL_MAX_REFS_PER_FORMULA", 20)
        self._depth = depth or _env_int("EXCEL_EXPAND_DEPTH", 1)
        if emit_dependencies is None:
            emit_dependencies = os.getenv("EXCEL_EMIT_DEPENDENCIES", "true").lower() != "false"
        self._emit_dependencies = emit_dependencies

    def convert(self, pdf_path: Path) -> str:
        path = Path(pdf_path)
        self.validate_path(path)
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise ValueError(
                f"excel converter handles {sorted(SUPPORTED_SUFFIXES)}, got {path.suffix!r}"
            )

        model = read_workbook(path, max_cells=self._max_cells)
        markdown = render_markdown(
            model,
            max_refs=self._max_refs,
            depth=self._depth,
            emit_dependencies=self._emit_dependencies,
        )
        formulas = sum(
            1 for sheet in model.sheets for cell in sheet.cells.values() if cell.formula
        )
        uncached = sum(
            1
            for sheet in model.sheets
            for cell in sheet.cells.values()
            if cell.formula and cell.value is None
        )
        logger.info(
            "excel: %s -> %d sheet(s), %d formula(s), %d without a cached result",
            path.name,
            len(model.sheets),
            formulas,
            uncached,
        )
        return markdown
