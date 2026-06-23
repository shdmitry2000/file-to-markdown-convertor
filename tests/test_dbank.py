"""Routing tests for the dbank converter (engines mocked — no docling/VLM/IO)."""
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.converters.dbank import DbankConverter, _heb_word_count, _heb_ratio

HEB = "בנקאות פתוחה בישראל מאפשרת ללקוחות גישה למידע פיננסי"  # 8 Hebrew words
ENG = "Open banking in Israel grants third parties access to financial data"


def _page(text, table=False):
    pg = MagicMock()
    pg.get_text.return_value = text
    tables = pg.find_tables.return_value
    if table:
        t = MagicMock(); t.row_count = 3; t.col_count = 4  # 12 cells -> routes to VLM
        tables.tables = [t]
    else:
        tables.tables = []
    return pg


def _fake_doc(pages):
    doc = MagicMock()
    doc.page_count = len(pages)
    doc.__getitem__.side_effect = lambda i: pages[i]
    doc.__enter__.return_value = doc
    doc.__exit__.return_value = False
    return doc


def _run(pages, docling_out):
    conv = DbankConverter()
    with patch("app.converters.dbank.fitz.open", return_value=_fake_doc(pages)), \
         patch.object(DbankConverter, "validate_path", return_value=None), \
         patch.object(DbankConverter, "_write_page", return_value=Path("/tmp/_x.pdf")), \
         patch.object(conv, "_docling_convert", return_value=docling_out) as dmock, \
         patch.object(conv, "_vlm_convert", return_value=HEB) as vmock:
        out = conv.convert(Path("/tmp/whatever.pdf"))
    return out, dmock, vmock


def test_pure_helpers():
    assert _heb_word_count(HEB) == 8
    assert _heb_ratio(HEB) > 0.9
    assert _heb_ratio(ENG) == 0.0


def test_prose_page_uses_docling():
    _, d, v = _run([_page(HEB)], docling_out=HEB)
    assert d.called and not v.called


def test_english_page_dropped_by_prefilter():
    out, d, v = _run([_page(ENG)], docling_out=HEB)
    assert out == "" and not d.called and not v.called


def test_table_page_routes_to_vlm():
    _, d, v = _run([_page(HEB, table=True)], docling_out=HEB)
    assert v.called and not d.called


def test_completeness_guard_falls_back_to_vlm():
    # docling returns almost no Hebrew -> below 85% of the page's text-layer words
    _, d, v = _run([_page(HEB)], docling_out="x")
    assert d.called and v.called
