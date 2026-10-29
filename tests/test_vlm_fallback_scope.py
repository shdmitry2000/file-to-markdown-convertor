"""The VLM fallback re-reads rendered PDF pages, so it runs for PDFs only.

A table-heavy docx (a Hebrew form: mostly `|`, `-` and blanks) reads as "unusable"
by letter ratio. Sending it to the VLM failed the whole file — the VLM opens only
PDFs, and on OCP its pod-lane client cannot even be built (no PROXY_AUTH_TOKEN).
"""
from __future__ import annotations

import pytest

from app.workers import worker

TABLE_FORM = "| שם | |\n|---|---|\n" + "| | |\n" * 100  # letters/len < 0.10


@pytest.fixture(autouse=True)
def fallback_on(monkeypatch):
    monkeypatch.setenv("VLM_FALLBACK_ENABLED", "true")


def test_a_table_heavy_form_reads_as_unusable():
    assert worker._docling_output_unusable(TABLE_FORM)


def test_a_docx_keeps_docling_output():
    assert worker._vlm_fallback_wanted("/x/301_טופס.docx", TABLE_FORM) is False


def test_a_pdf_with_a_broken_text_layer_still_goes_to_the_vlm():
    assert worker._vlm_fallback_wanted("/x/scan.PDF", TABLE_FORM) is True


def test_usable_output_never_goes_to_the_vlm():
    assert worker._vlm_fallback_wanted("/x/a.pdf", "plain readable text " * 20) is False


def test_disabled_fallback_never_goes_to_the_vlm(monkeypatch):
    monkeypatch.setenv("VLM_FALLBACK_ENABLED", "false")
    assert worker._vlm_fallback_wanted("/x/scan.pdf", TABLE_FORM) is False


def test_an_empty_non_pdf_still_fails_loudly():
    with pytest.raises(Exception, match="Unreadable document"):
        worker._vlm_fallback_wanted("/x/empty.docx", "  \n")
