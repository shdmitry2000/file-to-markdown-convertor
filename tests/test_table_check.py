"""Table check: numbers in a VLM table must sit in their row and column on the page."""
from __future__ import annotations

from app.converters.table_check import misplaced

# An RTL table as the page draws it (x, y in pt): the 2024 column is to the right
# of 2023, and the title repeats "2024" above the table.
RUNS = [
    ("2024", 250, 40),                                   # title: "דוח לשנת 2024"
    ("2024", 300, 100), ("2023", 200, 100),              # column headers
    ("2,505,293", 300, 120), ("1,994,001", 200, 120),
    ("1,715,080", 300, 140), ("1,375,592", 200, 140),
    ("790,213", 300, 160), ("618,409", 200, 160),
]


def _md(rows):
    head, *body = rows
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    return "\n".join(lines + ["| " + " | ".join(r) + " |" for r in body])


GOOD = [
    ["", "2024", "2023"],
    ["הכנסות", "2,505,293", "1,994,001"],
    ["עלות", "1,715,080", "1,375,592"],
    ["**רווח גולמי**", "**790,213**", "**618,409**"],
]


def test_a_faithful_table_passes():
    checked, problems = misplaced("דוח לשנת 2024\n\n" + _md(GOOD), RUNS)
    assert checked == 8 and problems == []


def test_two_values_swapped_in_a_row():
    rows = [r[:] for r in GOOD]
    rows[2][1], rows[2][2] = rows[2][2], rows[2][1]
    _, problems = misplaced(_md(rows), RUNS)
    assert "1715080 not in column 3" in problems and "1375592 not in column 2" in problems


def test_a_column_shifted_down_a_row():
    rows = [r[:] for r in GOOD]
    rows[1][2], rows[2][2], rows[3][2] = "**618,409**", "1,994,001", "1,375,592"
    _, problems = misplaced(_md(rows), RUNS)
    assert any("not in row" in p for p in problems)


def test_values_mirrored_under_unmoved_headers():
    # "2024" is also in the title: it is anchored by the copy inside the table's band
    rows = [GOOD[0]] + [[r[0], r[2], r[1]] for r in GOOD[1:]]
    _, problems = misplaced(_md(rows), RUNS)
    assert "column order breaks 0x" not in problems and problems


def test_a_whole_table_in_mirror_image_is_fine():
    # every value still under its own header: only the reading direction differs
    rows = [[r[0], r[2], r[1]] for r in GOOD]
    assert misplaced(_md(rows), RUNS)[1] == []


def test_numbers_inside_text_cells_are_not_anchored():
    rows = [r[:] for r in GOOD] + [["הערה: 2,505,293 ש\"ח", "", ""]]
    _, problems = misplaced(_md(rows), RUNS)
    assert problems == []


def test_page_without_a_table():
    assert misplaced("רק טקסט 2,505,293", RUNS) == (0, [])
