"""Typed model of a workbook — the shared vocabulary for the excel converter.

Deliberately free of openpyxl and of any service dependency: :mod:`reader` fills
it in, everything downstream (regions, labels, formulas, renderers) reads it, so
the pipeline can be tested without touching a file and reused outside this
service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# openpyxl caps sheets at 16384 columns; the letter conversion below is the
# standard bijective base-26 used by A1 notation.
_ALPHABET_SIZE = 26


def column_letter(col: int) -> str:
    """1 -> 'A', 27 -> 'AA'."""
    letters = ""
    while col > 0:
        col, remainder = divmod(col - 1, _ALPHABET_SIZE)
        letters = chr(ord("A") + remainder) + letters
    return letters


def column_index(letters: str) -> int:
    """'A' -> 1, 'AA' -> 27."""
    index = 0
    for char in letters.upper():
        index = index * _ALPHABET_SIZE + (ord(char) - ord("A") + 1)
    return index


def a1(row: int, col: int) -> str:
    return f"{column_letter(col)}{row}"


@dataclass(frozen=True)
class Rect:
    """Inclusive 1-based cell rectangle."""

    top: int
    left: int
    bottom: int
    right: int

    def contains(self, row: int, col: int) -> bool:
        return self.top <= row <= self.bottom and self.left <= col <= self.right

    @property
    def ref(self) -> str:
        return f"{a1(self.top, self.left)}:{a1(self.bottom, self.right)}"


@dataclass
class Cell:
    row: int
    col: int
    value: Any = None
    """Cached result for a formula cell, literal value otherwise. ``None`` for a
    formula whose result was never saved by Excel — never a substituted zero."""
    formula: str | None = None
    """Formula text without the leading ``=`` (``SUM(A1:A2)``)."""
    is_array_formula: bool = False
    number_format: str = "General"
    comment: str | None = None
    bold: bool = False
    filled: bool = False
    indent: int = 0
    outline_level: int = 0

    @property
    def ref(self) -> str:
        return a1(self.row, self.col)

    @property
    def is_blank(self) -> bool:
        return self.value is None and self.formula is None

    @property
    def is_text(self) -> bool:
        return isinstance(self.value, str) and self.formula is None

    @property
    def is_numeric(self) -> bool:
        return isinstance(self.value, (int, float)) and not isinstance(self.value, bool)


@dataclass
class TableDef:
    """An Excel Table (ListObject) — an author-declared header range."""

    name: str
    rect: Rect
    header_row_count: int
    columns: list[str]


@dataclass
class Sheet:
    name: str
    index: int
    rtl: bool = False
    hidden: bool = False
    cells: dict[tuple[int, int], Cell] = field(default_factory=dict)
    merged: list[Rect] = field(default_factory=list)
    tables: list[TableDef] = field(default_factory=list)
    freeze_row: int = 0
    """Last row of the frozen top pane — the author's own header boundary."""
    freeze_col: int = 0
    autofilter: Rect | None = None
    hidden_rows: set[int] = field(default_factory=set)
    hidden_cols: set[int] = field(default_factory=set)
    max_row: int = 0
    max_col: int = 0
    truncated: bool = False
    """True when the cell budget stopped the read before the sheet ended."""

    def cell(self, row: int, col: int) -> Cell | None:
        return self.cells.get((row, col))

    def merge_anchor(self, row: int, col: int) -> tuple[int, int] | None:
        """Top-left of the merged range covering (row, col), if any.

        Excel stores a merged range's value only in its anchor; the rest read
        blank. Callers resolving labels must follow this.
        """
        for rect in self.merged:
            if rect.contains(row, col):
                return rect.top, rect.left
        return None

    def effective_cell(self, row: int, col: int) -> Cell | None:
        """The cell, or its merge anchor's cell when the position is covered."""
        cell = self.cells.get((row, col))
        if cell is not None and not cell.is_blank:
            return cell
        anchor = self.merge_anchor(row, col)
        if anchor and anchor != (row, col):
            return self.cells.get(anchor)
        return cell


@dataclass
class WorkbookModel:
    sheets: list[Sheet] = field(default_factory=list)
    defined_names: dict[str, str] = field(default_factory=dict)
    """Workbook-level name -> target such as ``'הלוואות'!$C$8``."""
    source_name: str = ""

    def sheet(self, name: str) -> Sheet | None:
        for sheet in self.sheets:
            if sheet.name == name:
                return sheet
        return None
