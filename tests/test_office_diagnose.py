"""docling says "could not load document" for every Word file it cannot open;
the error must name the actual cause (2026-11-01: OCP .docx failures that the log
could not explain)."""

import io
import zipfile

import docx
import pytest

from app.converters.office_diagnose import is_load_failure, load_failure_cause

DOCLING_MSG = ("Conversion failed for: f.docx with status: failure. Errors: "
               "MsWordDocumentBackend could not load document with hash 2412a8fd")


def _valid(path):
    d = docx.Document()
    d.add_paragraph("טופס מילוי תחקיר")
    d.save(path)
    return path


def _rewrite(src, dst, fn):
    with zipfile.ZipFile(src) as zi:
        files = {n: zi.read(n) for n in zi.namelist()}
    with zipfile.ZipFile(dst, "w") as zo:
        for n, data in fn(files).items():
            zo.writestr(n, data)
    return dst


def test_docling_message_is_a_load_failure():
    assert is_load_failure(DOCLING_MSG)
    assert not is_load_failure("Conversion exceeded 600s timeout and was terminated")


def test_valid_docx_has_no_cause(tmp_path):
    assert load_failure_cause(str(_valid(tmp_path / "ok.docx"))) is None


def test_encrypted_or_legacy_doc_named(tmp_path):
    p = tmp_path / "labelled.docx"
    p.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1") + b"\0" * 512)
    assert "encrypted" in load_failure_cause(str(p))


def test_not_a_zip_named(tmp_path):
    p = tmp_path / "broken.docx"
    p.write_bytes(b"garbage" * 100)
    assert "not a zip" in load_failure_cause(str(p))


def test_macro_enabled_saved_as_docx_names_the_content_type(tmp_path):
    main = b"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
    macro = b"application/vnd.ms-word.document.macroEnabled.main+xml"

    def as_docm(f):
        f["[Content_Types].xml"] = f["[Content_Types].xml"].replace(main, macro)
        return f

    p = _rewrite(_valid(tmp_path / "v.docx"), tmp_path / "form.docx", as_docm)
    cause = load_failure_cause(str(p))
    assert cause.startswith("ValueError") and "macroEnabled" in cause
    assert str(tmp_path) not in cause  # the full path no longer eats the message


def test_missing_part_named(tmp_path):
    def drop_styles(f):
        f.pop("word/styles.xml")
        return f

    p = _rewrite(_valid(tmp_path / "v.docx"), tmp_path / "nostyles.docx", drop_styles)
    assert "word/styles.xml" in load_failure_cause(str(p))


def test_pdf_is_not_judged(tmp_path):
    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF-1.7 broken")
    assert load_failure_cause(str(p)) is None


def test_job_error_adds_the_cause_and_logs_the_traceback(tmp_path, caplog):
    from app.workers.worker import _job_error

    p = tmp_path / "labelled.docx"
    p.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1") + b"\0" * 512)
    with caplog.at_level("ERROR"):
        msg = _job_error("cid", str(p), ("error", DOCLING_MSG, "Traceback: here"))
    assert msg.startswith(DOCLING_MSG) and "cause: not an Office Open XML package" in msg
    assert "Traceback: here" in caplog.text
