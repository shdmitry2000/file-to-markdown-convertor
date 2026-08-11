"""Cell value -> display string.

Excel stores a number and a display format separately: ``0.032`` shown as
``3.2%``, ``850000`` shown as ``850,000 ₪``. Retrieval answers quote what the
user sees, so the format is applied here rather than dumping the raw float.
"""

from __future__ import annotations

import datetime as dt

NO_RESULT = "(no cached result)"
"""Formula whose result Excel never saved. Stated, never guessed at."""


def _trim_number(value: float) -> str:
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        return str(value)
    if float(value).is_integer():
        return f"{int(value):,}"
    rounded = round(float(value), 4)
    return f"{rounded:,.4f}".rstrip("0").rstrip(".")


def format_value(value: object, number_format: str = "General") -> str:
    """Render *value* the way the sheet displays it."""
    if value is None:
        return NO_RESULT
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, dt.datetime):
        if value.time() == dt.time(0, 0):
            return value.strftime("%d/%m/%Y")
        return value.strftime("%d/%m/%Y %H:%M")
    if isinstance(value, dt.date):
        return value.strftime("%d/%m/%Y")
    if isinstance(value, dt.time):
        return value.strftime("%H:%M")
    if isinstance(value, (int, float)):
        if "%" in (number_format or ""):
            return f"{_trim_number(float(value) * 100)}%"
        return _trim_number(float(value))
    text = str(value).strip()
    return " ".join(text.split())
