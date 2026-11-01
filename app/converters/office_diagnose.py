"""Why docling could not open a Word / PowerPoint file.

docling wraps every load failure in one message ("MsWordDocumentBackend could not
load document with hash …") and drops the exception behind it, so the log said a
file failed and never why. Seven different faults read the same: an encrypted
file, a .docm saved as .docx, a missing part, broken XML… This re-opens the file
the way docling does and names the actual cause.
"""

from __future__ import annotations

from pathlib import Path

#: First bytes of an OLE compound file: a legacy .doc/.ppt, or an Office file
#: encrypted by a password or a sensitivity label (AIP / Purview).
_OLE_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")

_LOAD_FAILURE_MARKERS = ("could not load document", "is not valid")

_WORD = (".docx", ".docm", ".dotx", ".dotm")
_POWERPOINT = (".pptx", ".pptm", ".potx")


def is_load_failure(message: str) -> bool:
    """True for docling's generic "could not open this file" errors."""
    return any(m in (message or "") for m in _LOAD_FAILURE_MARKERS)


def load_failure_cause(file_path: str) -> str | None:
    """The reason docling could not open `file_path`, or None when it opens fine
    (or is not a Word / PowerPoint file, which this does not judge)."""
    path = Path(file_path)
    if path.suffix.lower() not in _WORD + _POWERPOINT:
        return None
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError as exc:
        return f"file cannot be read: {exc}"
    if head == _OLE_MAGIC:
        return ("not an Office Open XML package: the file is encrypted (password or "
                "sensitivity label) or is an old binary .doc/.ppt renamed — save an "
                "unprotected copy as .docx/.pptx")
    if head[:2] != b"PK":
        return "not an Office Open XML package (not a zip file) — the file is corrupt or not a Word/PowerPoint file"
    try:
        if path.suffix.lower() in _POWERPOINT:
            from pptx import Presentation
            Presentation(str(path))
        else:
            from docling.backend.msword_backend import MsWordDocumentBackend
            MsWordDocumentBackend.load_msword_file(path, "diagnose")
    except Exception as exc:  # noqa: BLE001 — naming the failure is the whole point
        cause = exc.__cause__ or exc
        # python-docx puts the full path in front of the part that matters.
        text = str(cause).replace(str(path), path.name)
        return f"{type(cause).__name__}: {text[:300]}"
    return None
