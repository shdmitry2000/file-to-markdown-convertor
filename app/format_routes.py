"""Which converter a given file format needs.

Kept apart from :mod:`app.api.main` on purpose: that module binds ZeroMQ sockets
at import time, so anything importing it acquires a live socket. This has no
side effects and can be imported from tests and from either service half.
"""

from __future__ import annotations

from pathlib import Path

# Formats that must not go to docling. Spreadsheets have their own converter
# (formulas, per-region tables) and docling does not support .xls at all, so a
# health check that assumed docling would report on a pipeline the real ingest
# never uses. Mirrors _resolve_converter_type in the ingest client.
FORMAT_ROUTES: dict[str, str] = {
    ".xlsx": "excel",
    ".xlsm": "excel",
    ".xls": "markitdown",
}

DEFAULT_CONVERTER = "docling"


def converter_for(filename: str) -> str:
    """Converter to use for *filename*, by extension."""
    return FORMAT_ROUTES.get(Path(filename or "").suffix.lower(), DEFAULT_CONVERTER)
