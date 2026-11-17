"""The pod's memory during a conversion, docling's PDF options, and a worker that keeps serving.

On gke-dev (5.3 GB, 1.4 CPU) a 164-page report OOM-killed markdown-api: the whole pod
went, with the API and every caller's job state in it. Covered here:

  * docling's options on every PDF path: a bounded page queue and threads that follow
    the container's CPU limit (docling's defaults: 100 pages per stage, 4 threads);
  * the memory guard: a conversion that takes the pod past MEMORY_GUARD_PERCENT of
    its limit is stopped and fails as a document problem, before the kernel kills
    the pod;
  * vlm, slides and pymupdf unload the warm children first;
  * one task failing outside the converter fails that task, not the worker.
"""

import json
import multiprocessing
import time

import pytest

from app.converters import docling_options as do
from app.workers import worker as w
from tests import _timeout_targets as cold_targets
from tests import _warm_targets as targets


@pytest.fixture(autouse=True, scope="module")
def _force_spawn():
    try:
        multiprocessing.set_start_method("spawn", force=True)
    except RuntimeError:
        pass


# ---- docling options ---------------------------------------------------------------

@pytest.fixture
def clean_env(monkeypatch):
    for name in ("DOCLING_CPU_THREADS", "DOCLING_NUM_THREADS", "OMP_NUM_THREADS",
                 "DOCLING_QUEUE_MAX_SIZE", "MARKDOWN_EMBEDDED_WORKERS"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("cpus, threads", [(1.4, 1), (0.5, 1), (3.9, 3), (None, None)])
def test_threads_follow_the_container_cpu_limit(clean_env, monkeypatch, cpus, threads):
    monkeypatch.setattr(do, "cgroup_cpus", lambda: cpus)
    assert do.cpu_threads() == threads


@pytest.mark.parametrize("cpus, workers, threads", [(4.0, 4, 1), (8.0, 4, 2), (1.4, 2, 1), (4.0, 1, 4)])
def test_embedded_workers_share_the_cpus(clean_env, monkeypatch, cpus, workers, threads):
    monkeypatch.setattr(do, "cgroup_cpus", lambda: cpus)
    monkeypatch.setenv("MARKDOWN_EMBEDDED_WORKERS", str(workers))
    assert do.cpu_threads() == threads


def test_docling_cpu_threads_overrides(clean_env, monkeypatch):
    monkeypatch.setattr(do, "cgroup_cpus", lambda: 1.4)
    monkeypatch.setenv("DOCLING_CPU_THREADS", "2")
    assert do.cpu_threads() == 2


def test_doclings_own_variables_are_left_to_docling(clean_env, monkeypatch):
    monkeypatch.setattr(do, "cgroup_cpus", lambda: 1.4)
    monkeypatch.setenv("OMP_NUM_THREADS", "2")
    assert do.cpu_threads() is None


def test_options_carry_the_queue_and_the_threads(clean_env, monkeypatch):
    monkeypatch.setattr(do, "cgroup_cpus", lambda: 1.4)
    monkeypatch.setenv("DOCLING_QUEUE_MAX_SIZE", "8")
    opts = do.pdf_pipeline_options(do_ocr=False, do_table_structure=True)
    assert opts.queue_max_size == 8
    assert opts.accelerator_options.num_threads == 1
    assert opts.do_table_structure and not opts.do_ocr


def test_a_bad_value_falls_back_to_the_default(clean_env, monkeypatch):
    monkeypatch.setenv("DOCLING_QUEUE_MAX_SIZE", "lots")
    assert do.queue_max_size() == do.QUEUE_DEFAULT


# ---- the memory guard -------------------------------------------------------------

def test_guard_is_a_share_of_the_limit(monkeypatch):
    monkeypatch.setattr(w, "_cgroup_memory_limit_mb", lambda: 5000.0)
    monkeypatch.delenv("MEMORY_GUARD_PERCENT", raising=False)
    assert w._memory_guard_mb() == pytest.approx(4500)
    monkeypatch.setenv("MEMORY_GUARD_PERCENT", "0")
    assert w._memory_guard_mb() is None, "0 turns it off"
    monkeypatch.delenv("MEMORY_GUARD_PERCENT")
    monkeypatch.setattr(w, "_cgroup_memory_limit_mb", lambda: None)
    assert w._memory_guard_mb() is None, "no limit (a laptop): no guard"


def test_over_the_guard_reports_the_reading(monkeypatch):
    monkeypatch.setattr(w, "_memory_guard_mb", lambda: 4500.0)
    monkeypatch.setattr(w, "_cgroup_anon_mb", lambda: 4600.0)
    assert w._over_memory_guard() == (4600.0, 4500.0)
    monkeypatch.setattr(w, "_cgroup_anon_mb", lambda: 3000.0)
    assert w._over_memory_guard() is None


def _pod_processes(monkeypatch, rss):
    """The container's processes, pid -> RSS in MB, as psutil and _rss_mb see them."""
    import psutil
    from types import SimpleNamespace

    mb = 1048576
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: [
        SimpleNamespace(pid=pid, info={"memory_info": SimpleNamespace(rss=r * mb)})
        for pid, r in rss.items()])
    monkeypatch.setattr(w, "_rss_mb", lambda proc: rss[proc.pid])


@pytest.mark.parametrize("rss, stopped", [
    ({1: 3500.0, 2: 600.0, 9: 300.0}, {1}),            # a big job beside a small one
    ({1: 2000.0, 2: 1900.0, 9: 300.0}, {1}),           # two growing together: the larger
])
def test_with_several_workers_only_the_largest_job_is_stopped(monkeypatch, rss, stopped):
    from types import SimpleNamespace

    monkeypatch.setenv("MARKDOWN_EMBEDDED_WORKERS", "2")
    monkeypatch.setattr(w, "_memory_guard_mb", lambda: 4000.0)
    monkeypatch.setattr(w, "_cgroup_anon_mb", lambda: 4200.0)
    _pod_processes(monkeypatch, rss)
    assert {pid for pid in (1, 2) if w._over_memory_guard(SimpleNamespace(pid=pid))} == stopped


def test_anon_memory_is_read_on_cgroup_v1_too(monkeypatch, tmp_path):
    """On cgroup v1 the guard read nothing and was silently off."""
    v1 = tmp_path / "memory.stat"
    v1.write_text("cache 104857600\nrss 1048576\ntotal_cache 104857600\ntotal_rss 3145728000\n")
    monkeypatch.setattr(w, "_MEMORY_STAT", ((str(tmp_path / "missing"), "anon"), (str(v1), "total_rss")))
    assert w._cgroup_anon_mb() == pytest.approx(3000)


def test_a_warm_child_exits_when_its_worker_died_before_it_armed(monkeypatch):
    """The check compared the reaper's pid with itself: a child spawned by a worker
    that died at once stayed behind with its ~1 GB."""
    import ctypes
    import os
    from types import SimpleNamespace

    class _Exit(BaseException):  # os._exit never returns; nothing may catch it
        pass

    def exit_(code):
        raise _Exit(code)

    armed = []
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: SimpleNamespace(prctl=lambda *a: armed.append(a)))
    monkeypatch.setattr(multiprocessing, "parent_process", lambda: SimpleNamespace(pid=os.getppid() + 1))
    monkeypatch.setattr(os, "_exit", exit_)
    with pytest.raises(_Exit):
        w._die_with_parent()
    assert armed, "the death signal was not armed"
    monkeypatch.setattr(multiprocessing, "parent_process", lambda: SimpleNamespace(pid=os.getppid()))
    w._die_with_parent()  # the worker is alive: the child stays


def test_the_warm_budget_is_shared_by_the_workers_in_the_pod(monkeypatch):
    monkeypatch.delenv("WARM_MEMORY_BUDGET_MB", raising=False)
    monkeypatch.setattr(w, "_cgroup_memory_limit_mb", lambda: 6000.0)
    monkeypatch.setenv("MARKDOWN_EMBEDDED_WORKERS", "2")
    assert w._warm_memory_budget_mb() == pytest.approx(2100)
    monkeypatch.delenv("MARKDOWN_EMBEDDED_WORKERS")
    assert w._warm_memory_budget_mb() == pytest.approx(4200)


def test_a_failing_reading_never_fails_the_job(monkeypatch):
    monkeypatch.setattr(w, "_memory_guard_mb", lambda: 1 / 0)
    assert w._over_memory_guard() is None


def test_warm_job_over_the_guard_is_stopped_and_not_retryable(monkeypatch):
    monkeypatch.setattr(w, "_warm_docling_loop", targets.ready_then_hang)
    monkeypatch.setattr(w, "_over_memory_guard", lambda proc=None: (4600.0, 4500.0))
    child = w._WarmDoclingWorker()
    started = time.monotonic()
    with pytest.raises(MemoryError) as exc:
        child.convert("/tmp/x.pdf", "conv-mem", False, True, 300)
    assert time.monotonic() - started < 30, "the guard waited for the timeout"
    assert "too large" in str(exc.value) and "4600" in str(exc.value)
    assert child._proc is None, "the child holding the memory was not stopped"


def test_cold_job_over_the_guard_is_stopped(monkeypatch, tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(w, "_over_memory_guard", lambda proc=None: (4600.0, 4500.0))
    started = time.monotonic()
    with pytest.raises(MemoryError):
        w._run_converter_with_timeout(str(pdf), "c-cold", "pymupdf", 300,
                                      _target=cold_targets.sleep_forever)
    assert time.monotonic() - started < 30


@pytest.mark.parametrize("converter, stops", [("vlm", True), ("slides", True), ("pymupdf", True),
                                               ("excel", False), ("markitdown", False)])
def test_heavy_subprocess_converters_unload_the_warm_children(monkeypatch, tmp_path, converter, stops):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    seen = []
    monkeypatch.setattr(w, "_stop_other_warm", seen.append)
    monkeypatch.setattr(w, "_slide_deck_reason", lambda path: None)
    monkeypatch.setattr(w, "_convert_in_subprocess", cold_targets.succeed_immediately)
    w._run_converter_with_timeout(str(pdf), "c1", converter, 30)
    assert seen == ([None] if stops else [])


# ---- the worker loop --------------------------------------------------------------

class _Done(Exception):
    pass


class _Results:
    def __init__(self):
        self.sent = []

    def send_json(self, msg):
        self.sent.append(msg)


class _Beat:
    def __init__(self, *a, **k):
        pass

    def start(self):
        pass

    def stop(self):
        pass


def test_a_task_that_fails_outside_the_converter_does_not_end_the_worker(monkeypatch):
    """A storage error while staging the file used to leave _serve: the worker died."""
    from app import storage

    class _Store:
        def materialize(self, key):
            raise RuntimeError("object store unreachable")

    tasks = [{"conversion_id": "c1", "file_key": "a/b.pdf"},
             {"conversion_id": "c2", "file_key": "a/c.pdf"}]

    def next_task(sock, worker_id, finished):
        if not tasks:
            raise _Done
        return tasks.pop(0)

    monkeypatch.setattr(w, "_next_task", next_task)
    monkeypatch.setattr(w, "_Heartbeat", _Beat)
    monkeypatch.setattr(w, "is_cancelled", lambda cid: False)
    monkeypatch.setattr(storage, "file_store", lambda: _Store())
    results = _Results()
    with pytest.raises(_Done):
        w._serve(None, None, results, "addr", "w1", [None])
    failed = {m["conversion_id"]: m for m in results.sent}
    assert set(failed) == {"c1", "c2"}, "the second task never ran"
    assert all(m["status"] == "failed" and "object store unreachable" in m["error"]
               for m in failed.values())
    json.dumps(results.sent)  # what goes on the wire is JSON


def test_a_failed_chunk_task_is_reported_as_a_chunk_result(monkeypatch):
    """The API applies a chunk result only with type=chunk; without it the caller of
    /chunk-async waited for the watchdog."""
    tasks = [{"type": "chunk", "chunk_id": "k1"}]  # no file_path: fails before chunking

    def next_task(sock, worker_id, finished):
        if not tasks:
            raise _Done
        return tasks.pop(0)

    monkeypatch.setattr(w, "_next_task", next_task)
    monkeypatch.setattr(w, "_Heartbeat", _Beat)
    results = _Results()
    with pytest.raises(_Done):
        w._serve(None, None, results, "addr", "w1", [None])
    assert results.sent == [{"task_id": "k1", "chunk_id": "k1", "status": "failed",
                             "error": "worker error: 'file_path'", "type": "chunk"}]
