"""Formula-aware Excel conversion.

Pipeline: :mod:`reader` (openpyxl -> :mod:`model`) -> :mod:`regions` (find the
tables) -> :mod:`labels` (name each cell from workbook metadata) ->
:mod:`formulas` (expand references, build the dependency graph) ->
:mod:`render_md`. Only :mod:`converter` touches the service; everything below it
is plain Python and reusable on its own.
"""

from .converter import ExcelConverter

__all__ = ["ExcelConverter"]
