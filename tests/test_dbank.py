"""Routing tests for the dbank converter (engines mocked — no docling/VLM/IO)."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.converters.dbank import (
    DbankConverter, _control_ratio, _heb_ratio, _heb_word_count, _word_precision,
)

HEB = "בנקאות פתוחה בישראל מאפשרת ללקוחות גישה למידע פיננסי"  # 8 Hebrew words
ENG = "Open banking in Israel grants third parties access to financial data"
# A text layer from a broken font encoding: glyph ids extracted as control characters.
# Long enough that a document of it plus one empty page stays above the scanned-doc
# threshold (avg 50 chars/page), so the empty page is judged on its own.
PROSE = " ".join([HEB] * 3)
GARBAGE = "\x01\x02\x03\x04\x05\x06\x07\x08 \x07 \x03 \x06 \x01\x02 $ \x01# \x02\x02\x02\x02"


def _page(text, image=False, rawdict=None):
    pg = MagicMock()
    # get_text() -> plain text; get_text("rawdict") -> glyph layout for the number check
    pg.get_text.side_effect = lambda *a, **k: (
        (rawdict or {"blocks": []}) if a and a[0] == "rawdict" else text)
    pg.rect.width = 595.0
    pg.rect.height = 842.0
    # big image (>10% of page) routes to VLM; empty list otherwise
    pg.get_image_info.return_value = [{"bbox": (0, 0, 500, 700)}] if image else []
    return pg


def _fake_doc(pages):
    doc = MagicMock()
    doc.page_count = len(pages)
    doc.__getitem__.side_effect = lambda i: pages[i]
    doc.__enter__.return_value = doc
    doc.__exit__.return_value = False
    return doc


def _run(pages, docling_ret, vlm_outputs=None):
    """docling_ret is the (markdown, n_tables) tuple _docling_convert returns;
    vlm_outputs, when given, are the successive transcribe_page results."""
    conv = DbankConverter()
    vlm = MagicMock()
    vlm.transcribe_page.return_value = HEB
    if vlm_outputs:
        vlm.transcribe_page.side_effect = list(vlm_outputs)
    with patch("app.converters.dbank.fitz.open", return_value=_fake_doc(pages)), \
         patch.object(DbankConverter, "validate_path", return_value=None), \
         patch.object(DbankConverter, "_write_page", return_value=Path("/tmp/_x.pdf")), \
         patch.object(DbankConverter, "_render", return_value="IMG_B64"), \
         patch.object(conv, "_docling_convert", return_value=docling_ret) as dmock, \
         patch.object(conv, "_ensure_vlm", side_effect=lambda: setattr(conv, "_vlm", vlm)):
        out = conv.convert(Path("/tmp/whatever.pdf"))
    return out, dmock, vlm


def test_pure_helpers():
    assert _heb_word_count(HEB) == 8
    assert _heb_ratio(HEB) > 0.9
    assert _heb_ratio(ENG) == 0.0
    assert _control_ratio(HEB) == 0.0 and _control_ratio("") == 0.0
    assert _control_ratio(GARBAGE) > 0.8


def test_prose_page_uses_docling():
    # docling detects no table + captures the text -> docling output kept
    _, d, vlm = _run([_page(HEB)], (HEB, 0))
    assert d.called and not vlm.transcribe_page.called


def test_english_page_is_converted():
    # no language filter: an English page takes the docling route like any other
    out, d, vlm = _run([_page(ENG)], (ENG, 0))
    assert d.called and not vlm.transcribe_page.called
    assert ENG in out


def test_unreadable_text_layer_routes_to_vlm():
    # broken font encoding -> the text layer is garbage -> VLM reads the image instead
    out, d, vlm = _run([_page(PROSE), _page(GARBAGE)], (PROSE, 0))
    assert vlm.transcribe_page.call_count == 1
    assert "<!-- page 2 -->" in out


def test_blank_page_is_skipped():
    out, _, vlm = _run([_page(PROSE), _page("")], (PROSE, 0))
    assert not vlm.transcribe_page.called
    assert "<!-- page 1 -->" in out and "<!-- page 2 -->" not in out


def test_image_only_page_routes_to_vlm():
    # no text but a large figure, in a document that is not scanned
    out, _, vlm = _run([_page(PROSE), _page("", image=True)], (PROSE, 0))
    assert vlm.transcribe_page.call_count == 1
    assert "<!-- page 2 -->" in out


def test_table_page_routes_to_vlm():
    # docling's layout model reports a table region -> page goes to the VLM
    _, d, vlm = _run([_page(HEB)], (HEB, 1))
    assert d.called and vlm.transcribe_page.called


def test_completeness_guard_falls_back_to_vlm():
    # docling returns almost no Hebrew -> below 85% of the page's text-layer words
    _, d, vlm = _run([_page(HEB)], ("x", 0))
    assert d.called and vlm.transcribe_page.called


# A Hebrew page with English terms, and docling's rendering of it with the English
# words reversed (seen on E062_bidi_mixed_rtl_ltr.pdf: "Unicode" -> "edocinU").
MIXED = HEB + " the Unicode BiDi algorithm handles direction for mixed text in every paragraph"
MIXED_REVERSED = HEB + " eht edocinU iDiB mhtirogla seldnah noitcerid rof dexim txet yreve hpargarap"


def test_word_precision():
    assert _word_precision(MIXED, MIXED) == 1.0
    assert _word_precision(MIXED_REVERSED, MIXED) < 0.2
    # docling's placeholders are not page words
    assert _word_precision(MIXED + " <!-- formula-not-decoded -->" * 5, MIXED) == 1.0
    # too few words in a script to judge it
    assert _word_precision(HEB + " edocinU", HEB) == 1.0


def test_clean_mixed_page_stays_on_docling():
    _, d, vlm = _run([_page(MIXED)], (MIXED, 0))
    assert d.called and not vlm.transcribe_page.called


def test_scrambled_latin_routes_to_vlm():
    _, d, vlm = _run([_page(MIXED)], (MIXED_REVERSED, 0))
    assert d.called and vlm.transcribe_page.call_count == 1


def test_scrambled_hebrew_routes_to_vlm():
    # same Hebrew word count (completeness passes) but the words are not the page's
    scrambled = " ".join(w[::-1] for w in PROSE.split())
    _, d, vlm = _run([_page(PROSE)], (scrambled, 0))
    assert d.called and vlm.transcribe_page.call_count == 1


def test_empty_vlm_answer_is_retried_once():
    out, _, vlm = _run([_page(HEB)], (HEB, 1), vlm_outputs=["", "| VLM table |"])
    assert vlm.transcribe_page.call_count == 2 and "| VLM table |" in out


def test_empty_vlm_answer_keeps_docling_text():
    # e.g. Vertex blocks the page (content_filter) on both attempts
    out, _, vlm = _run([_page(MIXED)], (MIXED_REVERSED, 0), vlm_outputs=["", ""])
    assert vlm.transcribe_page.call_count == 2 and MIXED_REVERSED in out


def test_empty_vlm_answer_on_image_page_leaves_page_empty():
    # docling never saw an image page, so there is nothing to fall back to
    out, _, vlm = _run([_page(PROSE), _page("", image=True)], (PROSE, 0), vlm_outputs=["", ""])
    assert vlm.transcribe_page.call_count == 2 and "<!-- page 2 -->" in out


def test_image_page_routes_to_vlm():
    # Hebrew prose page carrying a large figure -> VLM before docling is even run
    _, d, vlm = _run([_page(HEB, image=True)], (HEB, 0))
    assert vlm.transcribe_page.called and not d.called


def test_scanned_doc_routes_all_pages_to_vlm():
    # near-empty text layer on every page -> scanned -> all VLM, no docling, no drops
    out, d, vlm = _run([_page(""), _page("   ")], (HEB, 0))
    assert vlm.transcribe_page.call_count == 2 and not d.called
    assert out != ""  # nothing dropped


# -- number check on VLM pages ---------------------------------------------------

def _glyphs(*runs):
    """rawdict with one span per (text, x0) run on one line; 4pt per glyph."""
    spans = []
    for text, x0 in runs:
        chars = [{"c": ch, "bbox": (x0 + 4 * n, 100, x0 + 4 * n + 4, 110)}
                 for n, ch in enumerate(text)]
        spans.append({"chars": chars})
    return {"blocks": [{"lines": [{"spans": spans}]}]}


# The page stores 91,394 as two RTL runs in reverse order: ',91' is drawn at x=212,
# '394' at x=200 -> plain extraction reads '394,91'; sorting glyphs by x gives 91,394.
# 1,536,691 is drawn normally.
TABLE = _glyphs((",394", 212), ("91", 204), ("1,536,691", 300))


def test_number_helpers():
    from app.converters.number_check import not_on_page, numbers, page_numbers
    assert numbers("2024 | 1,536,691 | 12 | 3038,3037 | 4.32") == {1536691: 1, 3038: 1, 3037: 1}
    assert page_numbers(_page(HEB, rawdict=TABLE)) == {91394: 1, 1536691: 1}
    assert not_on_page("| 91,394 | 1,536,991 |", page_numbers(_page(HEB, rawdict=TABLE))) == [1536991]


def test_vlm_numbers_match_page_no_retry():
    out, _, vlm = _run([_page(HEB, rawdict=TABLE)], (HEB, 1),
                       vlm_outputs=["| 91,394 | 1,536,691 |"])
    assert vlm.transcribe_page.call_count == 1 and "dbank:" not in out


def test_wrong_number_fixed_by_retry():
    out, _, vlm = _run([_page(HEB, rawdict=TABLE)], (HEB, 1),
                       vlm_outputs=["| 91,394 | 1,536,991 |", "| 91,394 | 1,536,691 |"])
    assert vlm.transcribe_page.call_count == 2
    assert "1,536,691" in out and "dbank:" not in out


def test_wrong_number_flagged_when_retry_fails():
    out, _, vlm = _run([_page(HEB, rawdict=TABLE)], (HEB, 1),
                       vlm_outputs=["| 91,394 | 1,536,991 |", "| 91,394 | 1,536,901 |"])
    assert vlm.transcribe_page.call_count == 2
    assert "<!-- dbank: numbers not on the page: 1,536,991 -->" in out


def _grid(*runs):
    """rawdict with one line per (text, x0, y) run; 4pt per glyph."""
    lines = [{"spans": [{"chars": [{"c": ch, "bbox": (x0 + 4 * n, y, x0 + 4 * n + 4, y + 10)}
                                   for n, ch in enumerate(text)]}]} for text, x0, y in runs]
    return {"blocks": [{"lines": lines}]}


# 2024 column on the right (x=300), 2023 on the left (x=200), three rows.
GRID = _grid(("2024", 300, 100), ("2023", 200, 100),
             ("2,505,293", 300, 120), ("1,994,001", 200, 120),
             ("1,715,080", 300, 140), ("1,375,592", 200, 140))
GRID_OK = "| | 2024 | 2023 |\n|---|---|---|\n| a | 2,505,293 | 1,994,001 |\n| b | 1,715,080 | 1,375,592 |"
GRID_SWAPPED = "| | 2024 | 2023 |\n|---|---|---|\n| a | 2,505,293 | 1,994,001 |\n| b | 1,375,592 | 1,715,080 |"


def test_value_in_the_wrong_column_fixed_by_retry():
    # every number is on the page, so only the table check can see this
    out, _, vlm = _run([_page(HEB, rawdict=GRID)], (HEB, 1),
                       vlm_outputs=[GRID_SWAPPED, GRID_OK])
    assert vlm.transcribe_page.call_count == 2
    assert GRID_OK in out and "dbank:" not in out


def test_value_in_the_wrong_column_flagged_when_retry_fails():
    out, _, vlm = _run([_page(HEB, rawdict=GRID)], (HEB, 1),
                       vlm_outputs=[GRID_SWAPPED, GRID_SWAPPED])
    assert vlm.transcribe_page.call_count == 2
    assert "<!-- dbank: table cells not where the page has them: 1375592 not in column 2" in out


def test_scanned_page_is_not_number_checked():
    # no text layer -> nothing to check against -> one call, no flag
    out, _, vlm = _run([_page("")], (HEB, 0), vlm_outputs=["| 1,536,991 |"])
    assert vlm.transcribe_page.call_count == 1 and "dbank:" not in out


# -- page markers ------------------------------------------------------------------

def test_pages_carry_original_page_numbers_across_skipped_pages():
    # page 2 (blank) is skipped -> markers 1 and 3, in order
    out, _, _ = _run([_page(PROSE), _page(""), _page(PROSE)], (PROSE, 0),
                     vlm_outputs=None)
    assert out.startswith("<!-- page 1 -->")
    assert "<!-- page 2 -->" not in out and "<!-- page 3 -->" in out
    assert out.index("<!-- page 1 -->") < out.index("<!-- page 3 -->")


def test_vlm_page_keeps_its_page_number():
    out, _, vlm = _run([_page(HEB), _page(HEB, image=True)], (HEB, 0))
    assert vlm.transcribe_page.called
    assert out.index("<!-- page 1 -->") < out.index("<!-- page 2 -->")


def test_a_failed_page_goes_to_the_stronger_model():
    """The second attempt is the escalation model, not the same weak model again."""
    conv = DbankConverter()
    weak, strong = MagicMock(), MagicMock()
    weak.transcribe_page.return_value = GRID_SWAPPED
    strong.transcribe_page.return_value = GRID_OK

    def ensure():
        conv._vlm, conv._strong = weak, strong

    with patch("app.converters.dbank.fitz.open", return_value=_fake_doc([_page(HEB, rawdict=GRID)])), \
         patch.object(DbankConverter, "validate_path", return_value=None), \
         patch.object(DbankConverter, "_write_page", return_value=Path("/tmp/_x.pdf")), \
         patch.object(DbankConverter, "_render", return_value="IMG_B64"), \
         patch.object(conv, "_docling_convert", return_value=(HEB, 1)), \
         patch.object(conv, "_ensure_vlm", side_effect=ensure):
        out = conv.convert(Path("/tmp/whatever.pdf"))
    assert weak.transcribe_page.call_count == 1 and strong.transcribe_page.call_count == 1
    assert GRID_OK in out and "dbank:" not in out


@pytest.mark.parametrize("pod, escalates", [
    ("vertex_ai/gemini-3.5-flash-lite", True),
    ("vertex_ai/gemini-3.8-flash", False),  # already the strong model: plain retry
])
def test_escalation_only_when_the_pod_runs_a_weaker_model(monkeypatch, pod, escalates):
    monkeypatch.setenv("VLM_MODEL", pod)
    built = []
    with patch("app.converters.vlm.VLMConverter.__init__",
               lambda self, **kw: built.append(kw)):
        conv = DbankConverter()
        conv._ensure_vlm()
    assert (conv._strong is not None) == escalates
    assert built[-1].get("model_name") == ("gemini-3.8-flash" if escalates else None)
