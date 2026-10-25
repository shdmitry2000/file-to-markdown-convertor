"""The warm docling child does not grow with the number of documents it converts.

The warm child lives for the whole life of the worker pod and every docling
conversion runs in it, so memory it keeps per document (a cached page image, a
result held by a callback) is a leak that ends in an OOM kill — and with it every
ingest the platform has queued. This converts the same small PDF many times in one
real warm child and compares its RSS after warm-up with its RSS at the end.

Real docling with its models: deselected by default, run with `pytest -m integration`
(and before any docling upgrade ships).
"""

import multiprocessing

import psutil
import pytest

from app.workers.worker import _WarmDoclingWorker

WARMUP, RUNS = 5, 30
MAX_GROWTH_MB = 150


def _pdf(tmp_path) -> str:
    import fitz

    doc = fitz.open()
    for n in range(3):
        page = doc.new_page()
        page.insert_text((72, 72), f"Page {n + 1}: memory soak", fontsize=14)
        for row in range(8):   # a ruled grid, so the table model runs too
            y = 120 + row * 24
            page.draw_line((72, y), (520, y))
            for col in range(4):
                page.insert_text((80 + col * 110, y + 16), f"r{row}c{col} {row * col}")
    path = tmp_path / "soak.pdf"
    doc.save(str(path))
    doc.close()
    return str(path)


@pytest.mark.integration
def test_rss_stays_flat_over_many_conversions(tmp_path):
    multiprocessing.set_start_method("spawn", force=True)
    pdf = _pdf(tmp_path)
    w = _WarmDoclingWorker()
    try:
        for i in range(WARMUP):
            result = w.convert(pdf, f"warm-{i}", False, True, 300)
        assert "memory soak" in result["markdown"]
        child = psutil.Process(w._proc.pid)
        after_warmup = child.memory_info().rss / 2**20

        for i in range(RUNS):
            w.convert(pdf, f"run-{i}", False, True, 300)
        assert w._proc is not None and w._proc.pid == child.pid, "the warm child was restarted mid-run"
        at_end = child.memory_info().rss / 2**20
    finally:
        w._kill()

    growth = at_end - after_warmup
    assert growth < MAX_GROWTH_MB, (
        f"warm child grew {growth:.0f} MB over {RUNS} conversions "
        f"({after_warmup:.0f} -> {at_end:.0f} MB)")
    # Shutting the worker down leaves no child behind, running or zombie.
    assert not child.is_running(), "the warm child outlived _kill()"
