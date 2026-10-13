"""Routing tests for the dbank converter (engines mocked — no docling/VLM/IO)."""
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.converters.dbank import DbankConverter, _heb_word_count, _heb_ratio

HEB = "בנקאות פתוחה בישראל מאפשרת ללקוחות גישה למידע פיננסי"  # 8 Hebrew words
ENG = "Open banking in Israel grants third parties access to financial data"


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


def test_prose_page_uses_docling():
    # docling detects no table + captures the text -> docling output kept
    _, d, vlm = _run([_page(HEB)], (HEB, 0))
    assert d.called and not vlm.transcribe_page.called


def test_english_page_dropped_by_prefilter():
    out, d, vlm = _run([_page(ENG)], (HEB, 0))
    assert out == "" and not d.called and not vlm.transcribe_page.called


def test_table_page_routes_to_vlm():
    # docling's layout model reports a table region -> page goes to the VLM
    _, d, vlm = _run([_page(HEB)], (HEB, 1))
    assert d.called and vlm.transcribe_page.called


def test_completeness_guard_falls_back_to_vlm():
    # docling returns almost no Hebrew -> below 85% of the page's text-layer words
    _, d, vlm = _run([_page(HEB)], ("x", 0))
    assert d.called and vlm.transcribe_page.called


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


def test_scanned_page_is_not_number_checked():
    # no text layer -> nothing to check against -> one call, no flag
    out, _, vlm = _run([_page("")], (HEB, 0), vlm_outputs=["| 1,536,991 |"])
    assert vlm.transcribe_page.call_count == 1 and "dbank:" not in out
