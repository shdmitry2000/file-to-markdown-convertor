"""
Number check for VLM-transcribed pages: every number the VLM writes must be on the page.

Why: on a 20-page benchmark of Hebrew financial tables (Tempo / Amram filings),
gemini-3.5-flash changed a digit in the same cell on two runs, with a different wrong
value each time (1,536,691 -> 1,536,991 / 1,536,901). A plausible wrong figure is the
worst failure for a bank, and nothing downstream can see it.

The reference is the page's own text layer — but NOT ``page.get_text()``: in many
Hebrew PDFs the digit groups of a number are stored as separate RTL runs, so plain
extraction returns ``394,91`` for 91,394. Digits are always left-to-right on the page,
so rebuilding each number from its glyph boxes sorted by x recovers it exactly (it
matched gemini-3.8-flash on all 1,236 numbers of the benchmark).

Only numbers >= 1000 that are not years are compared: small numbers (note refs, list
items, percentages) carry little risk and are too often reformatted by the VLM.
"""
from __future__ import annotations

import re
from collections import Counter

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_GROUPED = re.compile(r"\d{1,3}(,\d{3})+")
_NUMERIC_GLYPHS = "0123456789,.()"
_GAP = 2.5  # pt: a wider gap between glyphs on one line starts a new number


def numbers(text: str) -> Counter:
    """Values >= 1000 that are not years. A comma list ('3038,3037') is split."""
    out: Counter = Counter()
    for tok in _NUM.findall(text):
        tok = tok.strip(",")
        if "." in tok:
            continue
        parts = [tok] if ("," not in tok or _GROUPED.fullmatch(tok)) else tok.split(",")
        for p in parts:
            p = p.replace(",", "")
            if p.isdigit():
                v = int(p)
                if v >= 1000 and not 1990 <= v <= 2035:
                    out[v] += 1
    return out


def page_numbers(page) -> Counter:
    """The page's numbers, rebuilt from glyph boxes sorted left-to-right."""
    return numbers("\n".join(run for run, _x, _y in numeric_runs(page)))


def numeric_runs(page) -> list[tuple[str, float, float]]:
    """Each number on the page as (text, x centre, y centre), digits left-to-right."""
    chars = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                for c in span["chars"]:
                    if not c["c"].isspace():
                        y = round((c["bbox"][1] + c["bbox"][3]) / 2)
                        chars.append((y, c["bbox"][0], c["bbox"][2], c["c"]))
    chars.sort(key=lambda c: (c[0], c[1]))
    runs, cur = [], []

    def close():
        runs.append(("".join(x[3] for x in cur), (cur[0][1] + cur[-1][2]) / 2, cur[0][0]))

    for c in chars:
        numeric = c[3] in _NUMERIC_GLYPHS
        # Split on a non-digit glyph (Hebrew 'ו' in '101 ו-103'), a new line, a gap,
        # or x moving backwards (two lines a couple of points apart sort together).
        if cur and (not numeric or abs(c[0] - cur[-1][0]) > 2
                    or c[1] - cur[-1][2] > _GAP or c[1] < cur[-1][1]):
            close()
            cur = []
        if numeric:
            cur.append(c)
    if cur:
        close()
    return runs


def not_on_page(markdown: str, reference: Counter) -> list[int]:
    """Numbers the transcription contains (by count) that the page does not."""
    return sorted((numbers(markdown) - reference).elements())
