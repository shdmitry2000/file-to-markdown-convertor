"""Routing tests for the dbank converter (engines mocked — no docling/VLM/IO)."""
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.converters.dbank import DbankConverter, _heb_word_count, _heb_ratio

HEB = "בנקאות פתוחה בישראל מאפשרת ללקוחות גישה למידע פיננסי"  # 8 Hebrew words
ENG = "Open banking in Israel grants third parties access to financial data"


def _page(text, image=False):
    pg = MagicMock()
    pg.get_text.return_value = text
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


def _run(pages, docling_ret):
    """docling_ret is the (markdown, n_tables) tuple _docling_convert returns."""
    conv = DbankConverter()
    vlm = MagicMock()
    vlm.transcribe_page.return_value = HEB
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
