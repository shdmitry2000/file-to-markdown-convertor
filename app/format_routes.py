"""Which converter a given file format needs.

Kept apart from :mod:`app.api.main` on purpose: that module binds ZeroMQ sockets
at import time, so anything importing it acquires a live socket. This has no
side effects and can be imported from tests and from either service half.
"""

from __future__ import annotations

from pathlib import Path

# Formats with a converter of their own when the caller names none: workbooks
# (legacy .xls too, read with xlrd) go to the formula-aware excel converter.
# Everything else goes to docling; markitdown is not used. The ingest client's
# rule is shared/rag_core/converter_routing.py in the main repo.
FORMAT_ROUTES: dict[str, str] = {
    ".xlsx": "excel",
    ".xlsm": "excel",
    ".xls": "excel",
}

DEFAULT_CONVERTER = "docling"


def converter_for(filename: str) -> str:
    """Converter to use for *filename*, by extension."""
    return FORMAT_ROUTES.get(Path(filename or "").suffix.lower(), DEFAULT_CONVERTER)
