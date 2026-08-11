"""Turn a formula into a sentence, and record what feeds what.

``=SUM(C3:C7)`` sitting next to ``2,736,000`` tells a reader nothing about which
rows produced it. This module rewrites it as::

    סה"כ · יתרה = 2,736,000  ⟵  SUM(C3:C7)
        = הפועלים · משכנתא=850,000 + הפועלים · צרכנית=120,000 + ...

Reference parsing uses openpyxl's own formula tokenizer rather than a regex: it
handles quoted sheet names, structured table references, whole-column refs,
external workbooks and ``_xlfn.`` prefixes, none of which survive naive pattern
matching. References that cannot be resolved statically (``INDIRECT``, another
workbook) are labelled as such instead of being guessed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum

from openpyxl.formula.tokenizer import Token, Tokenizer

from .labels import LabelResolver
from .model import Sheet, WorkbookModel, a1, column_index
from .values import NO_RESULT, format_value

logger = logging.getLogger(__name__)

DEFAULT_MAX_REFS = 20
DEFAULT_DEPTH = 1

# Functions whose arguments read naturally as a list of contributing terms.
_ADDITIVE = {"SUM", "SUBTOTAL", "SUMPRODUCT"}
_AGGREGATES = _ADDITIVE | {"AVERAGE", "COUNT", "COUNTA", "MIN", "MAX", "MEDIAN"}
# Functions that build references at calculation time — unresolvable statically.
_DYNAMIC = {"INDIRECT", "OFFSET"}

_CELL = re.compile(r"^\$?([A-Z]{1,3})\$?(\d+)$")
_RANGE = re.compile(r"^\$?([A-Z]{1,3})\$?(\d+):\$?([A-Z]{1,3})\$?(\d+)$")
_WHOLE_COL = re.compile(r"^\$?([A-Z]{1,3}):\$?([A-Z]{1,3})$")
_WHOLE_ROW = re.compile(r"^\$?(\d+):\$?(\d+)$")
_STRUCTURED = re.compile(r"^([^\[\]]+)\[([^\]]*)\]$")
# Defined names start with a letter or underscore — Hebrew included, since \w
# is Unicode-aware — and never look like a cell address.
_NAMED = re.compile(r"^[^\W\d][\w.]*$")


class RefKind(str, Enum):
    CELL = "cell"
    RANGE = "range"
    WHOLE_COLUMN = "whole_column"
    WHOLE_ROW = "whole_row"
    NAMED = "named"
    STRUCTURED = "structured"
    EXTERNAL = "external"
    UNRESOLVED = "unresolved"


@dataclass
class Ref:
    raw: str
    kind: RefKind
    sheet: str | None = None
    cells: list[tuple[int, int]] = field(default_factory=list)
    truncated: bool = False

    @property
    def resolvable(self) -> bool:
        return bool(self.cells)


@dataclass
class FormulaInfo:
    sheet: str
    ref: str
    label: str
    formula: str
    result: str
    expression: str
    precedents: list[tuple[str, int, int]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _split_sheet(raw: str) -> tuple[str | None, str]:
    if "!" not in raw:
        return None, raw
    sheet, _, rest = raw.rpartition("!")
    return sheet.strip("'"), rest


class FormulaEngine:
    """Resolves and describes formulas across a whole workbook."""

    def __init__(
        self,
        model: WorkbookModel,
        resolvers: dict[str, LabelResolver],
        max_refs: int = DEFAULT_MAX_REFS,
        depth: int = DEFAULT_DEPTH,
    ) -> None:
        self.model = model
        self.resolvers = resolvers
        self.max_refs = max_refs
        self.depth = max(1, depth)
        # Rendering describes every formula and then walks the whole workbook
        # again for the dependency graph, so each formula is parsed several
        # times. Tokenizing is the expensive part; cache per (sheet, formula).
        self._ref_cache: dict[tuple[str, str], list[Ref]] = {}

    # -- reference parsing ---------------------------------------------------

    def parse_refs(self, formula: str, sheet: Sheet) -> list[Ref]:
        key = (sheet.name, formula)
        cached = self._ref_cache.get(key)
        if cached is not None:
            return cached

        refs: list[Ref] = []
        seen: set[str] = set()
        try:
            tokens = Tokenizer(f"={formula}").items
        except Exception:  # tokenizer is strict; a malformed formula is data, not a crash
            logger.debug("untokenizable formula on %s: %r", sheet.name, formula)
            self._ref_cache[key] = refs
            return refs
        for token in tokens:
            if token.type != Token.OPERAND or token.subtype != Token.RANGE:
                continue
            if token.value in seen:
                continue
            seen.add(token.value)
            refs.append(self._classify(token.value, sheet))
        self._ref_cache[key] = refs
        return refs

    def _classify(self, raw: str, sheet: Sheet) -> Ref:
        if raw.startswith("["):
            return Ref(raw=raw, kind=RefKind.EXTERNAL)

        structured = _STRUCTURED.match(raw)
        if structured:
            return self._structured_ref(raw, structured, sheet)

        sheet_name, body = _split_sheet(raw)
        target = self.model.sheet(sheet_name) if sheet_name else sheet
        if target is None:
            return Ref(raw=raw, kind=RefKind.EXTERNAL, sheet=sheet_name)

        cell = _CELL.match(body)
        if cell:
            column, row = cell.groups()
            return Ref(
                raw=raw,
                kind=RefKind.CELL,
                sheet=target.name,
                cells=[(int(row), column_index(column))],
            )

        rng = _RANGE.match(body)
        if rng:
            left, top, right, bottom = rng.groups()
            cells = self._expand(
                int(top), column_index(left), int(bottom), column_index(right)
            )
            return Ref(
                raw=raw,
                kind=RefKind.RANGE,
                sheet=target.name,
                cells=cells[: self.max_refs],
                truncated=len(cells) > self.max_refs,
            )

        whole_col = _WHOLE_COL.match(body)
        if whole_col:
            left, right = (column_index(part) for part in whole_col.groups())
            cells = [
                (row, col)
                for (row, col) in sorted(target.cells)
                if left <= col <= right
            ]
            return Ref(
                raw=raw,
                kind=RefKind.WHOLE_COLUMN,
                sheet=target.name,
                cells=cells[: self.max_refs],
                truncated=len(cells) > self.max_refs,
            )

        whole_row = _WHOLE_ROW.match(body)
        if whole_row:
            top, bottom = (int(part) for part in whole_row.groups())
            cells = [
                (row, col) for (row, col) in sorted(target.cells) if top <= row <= bottom
            ]
            return Ref(
                raw=raw,
                kind=RefKind.WHOLE_ROW,
                sheet=target.name,
                cells=cells[: self.max_refs],
                truncated=len(cells) > self.max_refs,
            )

        if _NAMED.match(body):
            return self._named_ref(raw, body)

        return Ref(raw=raw, kind=RefKind.UNRESOLVED, sheet=sheet_name)

    def _structured_ref(self, raw: str, match: re.Match, sheet: Sheet) -> Ref:
        table_name, column_name = match.groups()
        for candidate in self.model.sheets:
            for table in candidate.tables:
                if table.name != table_name:
                    continue
                first_data_row = table.rect.top + max(table.header_row_count, 1)
                if column_name and column_name in table.columns:
                    col = table.rect.left + table.columns.index(column_name)
                    cols = [col]
                else:
                    cols = list(range(table.rect.left, table.rect.right + 1))
                cells = [
                    (row, col)
                    for row in range(first_data_row, table.rect.bottom + 1)
                    for col in cols
                ]
                return Ref(
                    raw=raw,
                    kind=RefKind.STRUCTURED,
                    sheet=candidate.name,
                    cells=cells[: self.max_refs],
                    truncated=len(cells) > self.max_refs,
                )
        return Ref(raw=raw, kind=RefKind.UNRESOLVED)

    def _named_ref(self, raw: str, name: str) -> Ref:
        target = self.model.defined_names.get(name)
        if not target:
            return Ref(raw=raw, kind=RefKind.UNRESOLVED)
        sheet_name, body = _split_sheet(target)
        cell = _CELL.match(body)
        if not cell or self.model.sheet(sheet_name or "") is None:
            return Ref(raw=raw, kind=RefKind.NAMED, sheet=sheet_name)
        column, row = cell.groups()
        return Ref(
            raw=raw,
            kind=RefKind.NAMED,
            sheet=sheet_name,
            cells=[(int(row), column_index(column))],
        )

    def _expand(self, top: int, left: int, bottom: int, right: int) -> list[tuple[int, int]]:
        return [
            (row, col)
            for row in range(top, bottom + 1)
            for col in range(left, right + 1)
        ]

    # -- description ---------------------------------------------------------

    def describe(self, sheet: Sheet, row: int, col: int) -> FormulaInfo | None:
        cell = sheet.cells.get((row, col))
        if cell is None or not cell.formula:
            return None
        resolver = self.resolvers[sheet.name]
        label = resolver.label_for(row, col)
        refs = self.parse_refs(cell.formula, sheet)
        notes: list[str] = []

        function_names = self._functions(cell.formula)
        if function_names & _DYNAMIC:
            notes.append("builds its reference at calculation time — not resolved")
        if cell.is_array_formula:
            notes.append("array formula")
        if any(ref.kind is RefKind.EXTERNAL for ref in refs):
            notes.append("references another workbook — not resolved")
        if any(ref.truncated for ref in refs):
            notes.append(f"reference list truncated to {self.max_refs} cells")
        if cell.value is None:
            notes.append("no cached result in the file — Excel never saved one")

        expression = self._expression(sheet, cell.formula, refs, depth=self.depth, seen=set())
        precedents = [
            (ref.sheet or sheet.name, r, c) for ref in refs for (r, c) in ref.cells
        ]
        return FormulaInfo(
            sheet=sheet.name,
            ref=cell.ref,
            label=label.qualified,
            formula=cell.formula,
            result=format_value(cell.value, cell.number_format),
            expression=expression,
            precedents=precedents,
            notes=notes,
        )

    def _functions(self, formula: str) -> set[str]:
        try:
            tokens = Tokenizer(f"={formula}").items
        except Exception:
            return set()
        return {
            token.value.rstrip("(").removeprefix("_xlfn.").upper()
            for token in tokens
            if token.type == Token.FUNC and token.subtype == Token.OPEN
        }

    def _expression(
        self,
        sheet: Sheet,
        formula: str,
        refs: list[Ref],
        depth: int,
        seen: set[tuple[str, int, int]],
    ) -> str:
        """Rewrite *formula* with every reference replaced by label=value."""
        additive = self._additive_terms(sheet, formula, refs, depth, seen)
        if additive is not None:
            return additive

        rendered = formula
        for ref in sorted(refs, key=lambda r: -len(r.raw)):
            rendered = rendered.replace(ref.raw, self._render_ref(sheet, ref, depth, seen))
        return rendered

    def _additive_terms(
        self,
        sheet: Sheet,
        formula: str,
        refs: list[Ref],
        depth: int,
        seen: set[tuple[str, int, int]],
    ) -> str | None:
        """``SUM(C3:C7)`` -> ``a=1 + b=2 + ...`` when the call is the whole formula."""
        match = re.fullmatch(r"(?:_xlfn\.)?([A-Z]+)\((.*)\)", formula.strip(), re.IGNORECASE)
        if not match:
            return None
        name, inner = match.group(1).upper(), match.group(2)
        if name not in _ADDITIVE or not refs:
            return None
        if {ref.raw for ref in refs} != {part.strip() for part in inner.split(",")}:
            return None  # arguments are not plain references
        terms = [
            term
            for ref in refs
            for term in self._ref_terms(sheet, ref, depth, seen)
        ]
        if not terms:
            return None
        suffix = " + …" if any(ref.truncated for ref in refs) else ""
        return " + ".join(terms) + suffix

    def _ref_terms(
        self,
        sheet: Sheet,
        ref: Ref,
        depth: int,
        seen: set[tuple[str, int, int]],
    ) -> list[str]:
        terms: list[str] = []
        target = self.model.sheet(ref.sheet or sheet.name)
        if target is None:
            return terms
        resolver = self.resolvers.get(target.name)
        spans_many = len(ref.cells) > 1
        for (row, col) in ref.cells:
            cell = target.cells.get((row, col))
            if cell is None or cell.is_blank:
                continue
            # A wide reference (a range, a whole column) sweeps up header
            # captions; they are not contributing terms. A single-cell ref
            # pointing at a header is deliberate, so it stays.
            if spans_many and resolver is not None and resolver.is_header(row, col):
                continue
            terms.append(self._term(target, row, col, depth, seen))
        return terms

    def _term(
        self,
        sheet: Sheet,
        row: int,
        col: int,
        depth: int,
        seen: set[tuple[str, int, int]],
    ) -> str:
        cell = sheet.cells.get((row, col))
        resolver = self.resolvers.get(sheet.name)
        label = resolver.label_for(row, col).qualified if resolver else a1(row, col)
        value = format_value(cell.value if cell else None, cell.number_format if cell else "General")
        key = (sheet.name, row, col)
        if cell is not None and cell.formula and depth > 1 and key not in seen:
            nested_refs = self.parse_refs(cell.formula, sheet)
            nested = self._expression(sheet, cell.formula, nested_refs, depth - 1, seen | {key})
            return f"{label}={value} [{nested}]"
        return f"{label}={value}"

    def _render_ref(
        self,
        sheet: Sheet,
        ref: Ref,
        depth: int,
        seen: set[tuple[str, int, int]],
    ) -> str:
        if ref.kind is RefKind.EXTERNAL:
            return f"⟨{ref.raw}: another workbook⟩"
        if not ref.resolvable:
            return f"⟨{ref.raw}: unresolved⟩"
        if len(ref.cells) == 1 and ref.kind in (RefKind.CELL, RefKind.NAMED):
            row, col = ref.cells[0]
            target = self.model.sheet(ref.sheet or sheet.name) or sheet
            return f"({self._term(target, row, col, depth, seen)})"
        terms = self._ref_terms(sheet, ref, depth, seen)
        if not terms:
            return f"⟨{ref.raw}: empty⟩"
        suffix = ", …" if ref.truncated else ""
        return "{" + ", ".join(terms) + suffix + "}"

    # -- dependency graph ----------------------------------------------------

    def dependents(self) -> dict[tuple[str, int, int], list[tuple[str, int, int]]]:
        """Reverse edges: which formula cells consume each cell."""
        graph: dict[tuple[str, int, int], list[tuple[str, int, int]]] = {}
        for sheet in self.model.sheets:
            for (row, col), cell in sheet.cells.items():
                if not cell.formula:
                    continue
                for ref in self.parse_refs(cell.formula, sheet):
                    for (source_row, source_col) in ref.cells:
                        key = (ref.sheet or sheet.name, source_row, source_col)
                        graph.setdefault(key, []).append((sheet.name, row, col))
        return graph

    def label_of(self, sheet_name: str, row: int, col: int) -> str:
        resolver = self.resolvers.get(sheet_name)
        return resolver.label_for(row, col).qualified if resolver else a1(row, col)


__all__ = ["FormulaEngine", "FormulaInfo", "Ref", "RefKind", "NO_RESULT"]
