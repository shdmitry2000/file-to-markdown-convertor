"""docling's PDF pipeline options, the same on every path that builds a PDF converter.

Two docling defaults sized for a workstation broke a 5.3 GB / 1.4 CPU pod on a
164-page report:

  * ``queue_max_size`` 100 — each stage of docling's threaded page pipeline may hold
    100 pages, and a page keeps its image and parsed text until the last stage. The
    table stage is the slow one on CPU, so the stages before it run ahead and the
    pages pile up in memory in front of it.
  * ``num_threads`` 4 — layout and table models each run 4 torch threads, at the same
    time, on 1.4 CPUs: the threads fight for the quota and the job crawls.

DOCLING_QUEUE_MAX_SIZE and DOCLING_CPU_THREADS set them. Unset, the queue is
QUEUE_DEFAULT (10) pages and the threads follow the container's CPU limit (cgroup
cpu.max), shared by the embedded workers in it; with no limit (a laptop)
docling's own default applies, as do DOCLING_NUM_THREADS / OMP_NUM_THREADS,
which docling reads itself.
"""

from __future__ import annotations

import logging
import math
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Measured 2026-11-17 on the 164-page report, 1.4 CPU / 5 GB, 1 thread: queue 100
# peaked at 4,318 MB anon, 10 at 3,345 MB, for 12% more time (1,502 s -> 1,689 s).
QUEUE_DEFAULT = 10


def _int_env(name: str) -> int | None:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return None
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning("%s=%r is not a number; ignored", name, raw)
        return None


def cgroup_cpus() -> float | None:
    """CPUs the container may use (cgroup v2 cpu.max, then v1), or None when unlimited."""
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
        return None if quota == "max" else int(quota) / int(period)
    except (OSError, ValueError):
        pass
    try:
        quota = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text())
        period = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text())
        return None if quota <= 0 else quota / period
    except (OSError, ValueError):
        return None


def cpu_threads() -> int | None:
    """Threads per docling model, or None to leave docling's own choice."""
    threads = _int_env("DOCLING_CPU_THREADS")
    if threads:
        return threads
    if os.getenv("DOCLING_NUM_THREADS") or os.getenv("OMP_NUM_THREADS"):
        return None
    cpus = cgroup_cpus()
    if not cpus:
        return None
    # N embedded workers convert N documents at once in one container: each gets
    # its share of the CPUs, or 4 workers x 4 threads fight over 4 CPUs.
    return max(1, math.floor(cpus / _workers_in_container()))


def _workers_in_container() -> int:
    """MARKDOWN_EMBEDDED_WORKERS when the workers run inside the API pod (each
    inherits it), else 1 — a worker pod runs one."""
    try:
        return max(1, int(os.getenv("MARKDOWN_EMBEDDED_WORKERS", "1") or 1))
    except ValueError:
        return 1


def queue_max_size() -> int:
    return _int_env("DOCLING_QUEUE_MAX_SIZE") or QUEUE_DEFAULT


def pdf_pipeline_options(*, do_ocr: bool, do_table_structure: bool):
    from docling.datamodel.pipeline_options import (
        AcceleratorDevice, AcceleratorOptions, PdfPipelineOptions,
    )

    opts = PdfPipelineOptions()
    opts.do_ocr = do_ocr
    opts.do_table_structure = do_table_structure
    opts.queue_max_size = queue_max_size()
    threads = cpu_threads()
    if threads:
        opts.accelerator_options = AcceleratorOptions(num_threads=threads,
                                                      device=AcceleratorDevice.CPU)
    return opts
