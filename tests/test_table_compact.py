"""docling pads table cells to the column width; a long cell in a Word form made
most of the markdown padding and most chunks empty (2026-11-01, v2 on OCP)."""

import re

from app.converters.table_compact import compact_tables

PADDED = (
    "# טופס\n"
    "\n"
    "| שדה      | ערך" + " " * 300 + "|\n"
    "|----------|" + "-" * 303 + "|\n"
    "| תיאור    | " + "הממשק לא נשלח " * 21 + "  |\n"
    "| ריק      |" + " " * 303 + "|\n"
    "\n"
    "טקסט רגיל   עם רווחים\n"
)


def test_padding_is_gone_and_text_kept():
    out = compact_tables(PADDED)
    assert len(out) < len(PADDED) / 2
    assert re.sub(r"\s+", "", out.replace("|", "")).count("הממשקלאנשלח") == 21
    assert "| שדה | ערך |" in out
    assert "| ריק |  |" in out


def test_separator_keeps_three_dashes_for_every_table_reader():
    out = compact_tables("| a | b | c |\n|:---------|----------:|:------:|\n")
    assert out.splitlines()[1] == "| :--- | ---: | :---: |"


def test_escaped_pipe_stays_inside_its_cell():
    assert compact_tables(r"| a \| b   | c   |") == r"| a \| b | c |"


def test_non_table_lines_untouched():
    text = "טקסט רגיל   עם רווחים\n---\n| not closed"
    assert compact_tables(text) == text
