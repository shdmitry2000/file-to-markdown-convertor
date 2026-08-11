"""Give every cell a human-readable name, without asking a model.

A formula cell means nothing as ``C8``. It means something as
"סה"כ · יתרה (₪)". The partner's pipeline asks an LLM for that name; almost
always the workbook already states it, so this walks a ladder of sources from
most to least authoritative and records which rung answered:

    comment > defined_name > table > header_explicit > header_style
            > proximity > region > address

The recorded rung is the point of the exercise: :func:`coverage` reports how
often we fall all the way to ``address``, which is the only case an AI pass could
improve. If that number is ~0 on real workbooks, the AI rung is never built.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .model import Sheet, a1
from .regions import Region

RUNG_ORDER = [
    "comment",
    "defined_name",
    "table",
    "header_explicit",
    "header_style",
    "proximity",
    "region",
    "address",
]

# [$₪-he-IL] / [$$-en-US] style currency tokens, and bare symbols in a format.
_CURRENCY_TOKEN = re.compile(r"\[\$([^\-\]]+)")
_BARE_SYMBOL = re.compile(r"[₪$€£¥]")
_SHEET_REF = re.compile(r"^(?:'([^']+)'|([^!]+))!\$?([A-Z]+)\$?(\d+)$")


@dataclass
class Label:
    text: str
    rung: str
    row_label: str | None = None
    col_label: str | None = None
    unit: str | None = None

    @property
    def qualified(self) -> str:
        return f"{self.text} ({self.unit})" if self.unit else self.text


def unit_of(number_format: str) -> str | None:
    """Units carried by the number format — what the figure is measured in."""
    if not number_format or number_format == "General":
        return None
    if "%" in number_format:
        return "%"
    match = _CURRENCY_TOKEN.search(number_format)
    if match:
        return match.group(1)
    symbol = _BARE_SYMBOL.search(number_format)
    return symbol.group(0) if symbol else None


class LabelResolver:
    """Resolves labels for one sheet. Build once, query per cell."""

    def __init__(
        self,
        sheet: Sheet,
        regions: list[Region],
        defined_names: dict[str, str] | None = None,
    ) -> None:
        self.sheet = sheet
        self.regions = regions
        self._names_by_cell = self._index_defined_names(defined_names or {})

    # -- setup ---------------------------------------------------------------

    def _index_defined_names(self, defined_names: dict[str, str]) -> dict[tuple[int, int], str]:
        index: dict[tuple[int, int], str] = {}
        for name, target in defined_names.items():
            match = _SHEET_REF.match((target or "").strip())
            if not match:
                continue
            quoted, plain, column, row = match.groups()
            if (quoted or plain) != self.sheet.name:
                continue
            from .model import column_index

            index.setdefault((int(row), column_index(column)), name.replace("_", " "))
        return index

    def is_header(self, row: int, col: int) -> bool:
        """True when (row, col) is part of a header row — a caption, not data."""
        region = self.region_at(row, col)
        return bool(region and row in region.header_rows)

    def region_at(self, row: int, col: int) -> Region | None:
        for region in self.regions:
            if region.rect.contains(row, col):
                return region
        return None

    # -- the ladder ----------------------------------------------------------

    def label_for(self, row: int, col: int) -> Label:
        cell = self.sheet.cells.get((row, col))
        unit = unit_of(cell.number_format) if cell else None

        if cell and cell.comment:
            return Label(_clean(cell.comment), "comment", unit=unit)

        name = self._names_by_cell.get((row, col))
        if name:
            return Label(name, "defined_name", unit=unit)

        region = self.region_at(row, col)
        if region is None:
            return Label(f"{self.sheet.name}!{a1(row, col)}", "address", unit=unit)

        table_label = self._from_table(row, col)
        if table_label:
            return table_label

        col_label = self._column_label(region, col)
        row_label = self._row_label(region, row)
        if col_label or row_label:
            parts = [part for part in (row_label, col_label) if part]
            rung = "header_explicit" if region.header_source in _EXPLICIT else "header_style"
            if not col_label:
                rung = "proximity"
            return Label(
                " · ".join(parts),
                rung,
                row_label=row_label,
                col_label=col_label,
                unit=unit,
            )

        if region.title:
            return Label(f"{region.title} · {a1(row, col)}", "region", unit=unit)
        return Label(f"{self.sheet.name}!{a1(row, col)}", "address", unit=unit)

    def _from_table(self, row: int, col: int) -> Label | None:
        for table in self.sheet.tables:
            if not table.rect.contains(row, col) or not table.columns:
                continue
            header_bottom = table.rect.top + max(table.header_row_count, 1) - 1
            if row <= header_bottom:
                continue
            offset = col - table.rect.left
            if 0 <= offset < len(table.columns):
                return Label(f"{table.name} · {table.columns[offset]}", "table")
        return None

    def _column_label(self, region: Region, col: int) -> str | None:
        parts: list[str] = []
        for header_row in region.header_rows:
            cell = self.sheet.effective_cell(header_row, col)
            if cell is None or cell.is_blank:
                continue
            text = _clean(cell.value)
            if text and text not in parts:
                parts.append(text)
        return " > ".join(parts) if parts else None

    def _row_label(self, region: Region, row: int) -> str | None:
        parts: list[str] = []
        outer_is_fresh = False
        for label_col in region.label_cols:
            cell = self.sheet.effective_cell(row, label_col)
            if cell is not None and not cell.is_blank:
                text = _clean(cell.value)
                outer_is_fresh = True
            elif outer_is_fresh:
                # A broader label column restated itself on this row, so the
                # finer ones no longer apply. Carrying them down would label a
                # totals row with the last detail row's category.
                continue
            else:
                text = self._label_down_column(region, row, label_col)
            if text and text not in parts:
                parts.append(text)
        return " · ".join(parts) if parts else None

    def _label_down_column(self, region: Region, row: int, col: int) -> str | None:
        """Value at (row, col), carried down from the last filled cell above.

        Sparse label columns are normal — a bank name is typed once and left
        blank for its remaining rows, or merged across them.
        """
        cell = self.sheet.effective_cell(row, col)
        if cell is not None and not cell.is_blank:
            return _clean(cell.value)
        for above in range(row - 1, region.data_top - 1, -1):
            previous = self.sheet.effective_cell(above, col)
            if previous is not None and not previous.is_blank:
                return _clean(previous.value)
        return None


_EXPLICIT = {"table", "freeze", "autofilter"}


def _clean(value: object) -> str:
    return " ".join(str(value).split()).strip()


def coverage(labels: list[Label]) -> dict[str, int]:
    """Count labels per rung, strongest first — the AI-necessity metric."""
    counts = {rung: 0 for rung in RUNG_ORDER}
    for label in labels:
        counts[label.rung] = counts.get(label.rung, 0) + 1
    return {rung: count for rung, count in counts.items() if count}
