"""Find the tables inside a sheet, and where their headers and labels are.

A sheet is not a table. Real workbooks put several blocks on one sheet — side by
side, stacked, each with its own title and header. Flattening the whole grid into
one markdown table is what makes today's output unusable: the title row becomes
the header and the real header becomes data.

Regions are split on fully blank rows/columns, then each region is classified:
which rows are header (possibly several levels, e.g. ``2026`` merged above
``Q1..Q4``) and which leading columns hold row labels.

Explicit signals win over heuristics, in this order: Excel Table header range,
freeze panes, autofilter, then style/type analysis. RTL only affects rendering —
a Hebrew sheet still stores its row labels in the leftmost columns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .model import Rect, Sheet

# A row of mostly-text, styled cells reads as a header; these thresholds decide
# "mostly". Loose enough for a header that mixes text and years (2024, 2025).
_TEXT_SHARE = 0.6
_TITLE_SPAN_SHARE = 0.8
_MAX_HEADER_ROWS = 4


class Orientation(str, Enum):
    MATRIX = "matrix"      # header row(s) AND label column(s) — a cross-tab
    TABLE = "table"        # header row(s) only
    LIST = "list"          # label column(s) only
    FLAT = "flat"          # neither — bare grid


@dataclass
class Region:
    rect: Rect
    title: str | None = None
    header_rows: list[int] = field(default_factory=list)
    label_cols: list[int] = field(default_factory=list)
    header_source: str = "none"
    """Where the header rows came from: table / freeze / autofilter / style."""

    @property
    def data_top(self) -> int:
        return max([self.rect.top, *(r + 1 for r in self.header_rows)])

    @property
    def data_cols(self) -> list[int]:
        return [c for c in range(self.rect.left, self.rect.right + 1) if c not in self.label_cols]

    @property
    def orientation(self) -> Orientation:
        if self.header_rows and self.label_cols:
            return Orientation.MATRIX
        if self.header_rows:
            return Orientation.TABLE
        if self.label_cols:
            return Orientation.LIST
        return Orientation.FLAT


def _occupied(sheet: Sheet) -> set[tuple[int, int]]:
    """Positions that count as filled, with merged ranges covering their span.

    A merged title stores its text only in the anchor, so without this a merged
    banner would not connect the columns it visually spans.
    """
    filled = {pos for pos, cell in sheet.cells.items() if not cell.is_blank}
    for rect in sheet.merged:
        anchor = sheet.cells.get((rect.top, rect.left))
        if anchor is None or anchor.is_blank:
            continue
        for row in range(rect.top, rect.bottom + 1):
            for col in range(rect.left, rect.right + 1):
                filled.add((row, col))
    return filled


def _bands(values: set[int]) -> list[tuple[int, int]]:
    """Group sorted integers into runs of consecutive values."""
    bands: list[tuple[int, int]] = []
    for value in sorted(values):
        if bands and value == bands[-1][1] + 1:
            bands[-1] = (bands[-1][0], value)
        else:
            bands.append((value, value))
    return bands


def find_regions(sheet: Sheet) -> list[Region]:
    """Split *sheet* into rectangular regions and classify each one."""
    filled = _occupied(sheet)
    if not filled:
        return []

    regions: list[Region] = []
    for top, bottom in _bands({row for row, _ in filled}):
        band = {(r, c) for r, c in filled if top <= r <= bottom}
        for left, right in _bands({col for _, col in band}):
            rect = Rect(top=top, left=left, bottom=bottom, right=right)
            regions.append(_classify(sheet, rect))
    return regions


def _classify(sheet: Sheet, rect: Rect) -> Region:
    region = Region(rect=rect)
    top = rect.top

    title = _title_at(sheet, rect)
    if title is not None:
        region.title = title
        top += 1
        if top > rect.bottom:
            return region

    header_rows, source = _explicit_header_rows(sheet, rect, top)
    if not header_rows:
        header_rows, source = _detected_header_rows(sheet, rect, top), "style"
    region.header_rows = header_rows
    region.header_source = source if header_rows else "none"
    region.label_cols = _label_cols(sheet, rect, region.data_top)
    return region


def _title_at(sheet: Sheet, rect: Rect) -> str | None:
    """A lone cell on the top row, at the region's left edge or spanning it."""
    if rect.top >= rect.bottom:
        return None
    present = [
        cell
        for col in range(rect.left, rect.right + 1)
        if (cell := sheet.cells.get((rect.top, col))) is not None and not cell.is_blank
    ]
    if len(present) != 1:
        return None
    cell = present[0]
    if not isinstance(cell.value, str):
        return None
    width = rect.right - rect.left + 1
    span = 1
    for merged in sheet.merged:
        if merged.top == cell.row and merged.left == cell.col:
            span = merged.right - merged.left + 1
    starts_left = cell.col == rect.left
    spans_region = span >= max(2, int(width * _TITLE_SPAN_SHARE))
    if starts_left or spans_region:
        return str(cell.value).strip()
    return None


def _explicit_header_rows(sheet: Sheet, rect: Rect, top: int) -> tuple[list[int], str]:
    """Header rows the author declared: Table range, freeze panes, autofilter."""
    for table in sheet.tables:
        if table.header_row_count and top <= table.rect.top <= rect.bottom:
            if rect.contains(table.rect.top, table.rect.left):
                rows = list(range(table.rect.top, table.rect.top + table.header_row_count))
                return rows, "table"
    if sheet.freeze_row and top <= sheet.freeze_row <= rect.bottom:
        return list(range(top, sheet.freeze_row + 1)), "freeze"
    if sheet.autofilter and rect.contains(sheet.autofilter.top, sheet.autofilter.left):
        if sheet.autofilter.top >= top:
            return [sheet.autofilter.top], "autofilter"
    return [], "none"


def _row_cells(sheet: Sheet, row: int, rect: Rect) -> list:
    return [
        cell
        for col in range(rect.left, rect.right + 1)
        if (cell := sheet.cells.get((row, col))) is not None and not cell.is_blank
    ]


def _looks_like_header(sheet: Sheet, row: int, rect: Rect) -> bool:
    cells = _row_cells(sheet, row, rect)
    if not cells:
        return False
    if any(cell.formula for cell in cells):
        return False
    styled = sum(cell.bold or cell.filled for cell in cells) / len(cells)
    textual = sum(cell.is_text for cell in cells) / len(cells)
    if styled >= _TEXT_SHARE and textual > 0:
        return True
    if textual < _TEXT_SHARE:
        return False
    # Unstyled but all-text row counts as a header only if what follows differs —
    # otherwise it is just the first row of a text column.
    below = _row_cells(sheet, row + 1, rect)
    return bool(below) and sum(cell.is_numeric for cell in below) / len(below) >= _TEXT_SHARE


def _spans_horizontally(sheet: Sheet, row: int, rect: Rect) -> bool:
    """A merged run across columns marks an upper header level (``2026``)."""
    return any(
        merged.top == row and merged.right > merged.left and rect.contains(row, merged.left)
        for merged in sheet.merged
    )


def _detected_header_rows(sheet: Sheet, rect: Rect, top: int) -> list[int]:
    primary = None
    for row in range(top, min(top + _MAX_HEADER_ROWS, rect.bottom) + 1):
        if _looks_like_header(sheet, row, rect):
            primary = row
            break
    if primary is None:
        return []
    rows = [primary]
    # Grow upward over spanning levels, downward over further header-ish rows.
    row = primary - 1
    while row >= top and _spans_horizontally(sheet, row, rect):
        rows.insert(0, row)
        row -= 1
    row = primary + 1
    while (
        row <= rect.bottom
        and len(rows) < _MAX_HEADER_ROWS
        and _looks_like_header(sheet, row, rect)
        and _spans_horizontally(sheet, primary, rect)
    ):
        rows.append(row)
        row += 1
    return rows


def _label_cols(sheet: Sheet, rect: Rect, data_top: int) -> list[int]:
    """Leading columns whose data cells are mostly text — the row labels."""
    label_cols: list[int] = []
    for col in range(rect.left, rect.right + 1):
        cells = [
            cell
            for row in range(data_top, rect.bottom + 1)
            if (cell := sheet.cells.get((row, col))) is not None and not cell.is_blank
        ]
        if not cells:
            # An empty leading column inside the region: keep scanning, it may
            # just be a spacer before the label column.
            if not label_cols:
                continue
            break
        if sum(cell.is_text for cell in cells) / len(cells) >= _TEXT_SHARE:
            label_cols.append(col)
        else:
            break
    return label_cols
