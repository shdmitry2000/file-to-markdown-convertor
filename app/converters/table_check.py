"""
Table check for VLM-transcribed pages: every number must sit in the right row and column.

Why: the number check (number_check.py) proves each number the VLM wrote is on the
page, not that it is in the right cell. A value in the neighbouring column, a row
shifted by one, or number columns mirrored under unmoved headers all pass it, and
in a Hebrew financial table that is a wrong figure for the reader.

The page knows where every number is: rebuilt from glyph boxes (numeric_runs), each
has an x and a y. For each markdown table, a number that is unique both on the page
and in the transcription is placed on the page, and then:

- column: its x must be nearest the median x of its own markdown column;
- row: its y must be nearest the median y of its own markdown row;
- order: the columns' median x must run one way (RTL or LTR — either is fine, a
  whole table read in mirror image keeps every value under its own header).

Only unique numbers are used, so a value repeated on the page never anchors a
wrong position. Tolerances are in PDF points.
"""
from __future__ import annotations

import re
from collections import Counter
from statistics import median

_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?")
_NUMBER_CELL = re.compile(r"[(\-−]?\s*\d[\d,]*(?:\.\d+)?\s*\)?\s*%?")
_COL_TOL = 10.0  # pt: how much nearer another column a number must be to count
_ROW_TOL = 3.0   # pt: the same for rows (lines are ~10 pt apart)
_HEADER_REACH = 40.0  # pt above the first placed number where column headers sit


def _key(token: str) -> str | None:
    """'1,536,691' -> '1536691'; '25.64' stays. Too short to be unique -> None."""
    k = token.strip(",.").replace(",", "")
    return k if len(k.replace(".", "")) >= 3 else None


def _tables(markdown: str) -> list[list[list[str]]]:
    """GFM tables as rows of cells; separator rows dropped."""
    tables, cur = [], []
    for line in markdown.splitlines() + [""]:
        s = line.strip()
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                cur.append(cells)
        elif cur:
            tables.append(cur)
            cur = []
    return tables


def _misplaced(values: list[float], groups: list[int], tol: float) -> list[int]:
    """Indexes whose value is nearer another group's median than its own (by > tol)."""
    by: dict[int, list[float]] = {}
    for v, g in zip(values, groups):
        by.setdefault(g, []).append(v)
    if len(by) < 2:
        return []
    med = {g: median(vs) for g, vs in by.items()}
    out = []
    for i, (v, g) in enumerate(zip(values, groups)):
        nearest = min(med, key=lambda h: abs(v - med[h]))
        if nearest != g and abs(v - med[g]) - abs(v - med[nearest]) > tol:
            out.append(i)
    return out


def _order_breaks(values: list[float], groups: list[int], tol: float) -> int:
    """Adjacent numeric columns whose median x runs against the table's main direction."""
    by: dict[int, list[float]] = {}
    for v, g in zip(values, groups):
        by.setdefault(g, []).append(v)
    meds = [median(by[g]) for g in sorted(by)]
    steps = [b - a for a, b in zip(meds, meds[1:]) if abs(b - a) > tol]
    up = sum(s > 0 for s in steps)
    return min(up, len(steps) - up)


def misplaced(markdown: str, runs: list[tuple[str, float, float]]) -> tuple[int, list[str]]:
    """(numbers checked, problems) for the markdown tables against the page's runs."""
    where: dict[str, list[tuple[float, float]]] = {}
    for text, x, y in runs:
        for tok in _TOKEN.findall(text):
            if k := _key(tok):
                where.setdefault(k, []).append((x, y))

    checked, problems = 0, []
    for table in _tables(markdown):
        md_count = Counter(k for row in table for cell in row
                           for t in _TOKEN.findall(cell) if (k := _key(t)))
        # A number inside text ("258 יח"ד", "נתניה 42101", a dated header) sits
        # wherever the text wraps; only a number-only cell is anchored.
        cells = [(r, c, k) for r, row in enumerate(table) for c, cell in enumerate(row)
                 if _NUMBER_CELL.fullmatch(cell.replace("*", "").strip())
                 for tok in _TOKEN.findall(cell) if (k := _key(tok)) and md_count[k] == 1]
        placed = {(r, c, k): where[k][0] for r, c, k in cells if len(where.get(k, ())) == 1}
        if placed:
            # A value also printed elsewhere on the page ("2024" in the title and in
            # the column header) is anchored when one copy lies in the table's band.
            top = min(y for _x, y in placed.values()) - _HEADER_REACH
            bottom = max(y for _x, y in placed.values()) + _ROW_TOL
            for r, c, k in cells:
                inside = [p for p in where.get(k, ()) if top <= p[1] <= bottom]
                if (r, c, k) not in placed and len(inside) == 1:
                    placed[(r, c, k)] = inside[0]
        keys = [k for _r, _c, k in placed]
        xs = [p[0] for p in placed.values()]
        ys = [p[1] for p in placed.values()]
        cols = [c for _r, c, _k in placed]
        rows = [r for r, _c, _k in placed]
        checked += len(keys)
        for i in _misplaced(xs, cols, _COL_TOL):
            problems.append(f"{keys[i]} not in column {cols[i] + 1}")
        for i in _misplaced(ys, rows, _ROW_TOL):
            problems.append(f"{keys[i]} not in row {rows[i] + 1}")
        if breaks := _order_breaks(xs, cols, _COL_TOL):
            problems.append(f"column order breaks {breaks}x")
    return checked, problems
