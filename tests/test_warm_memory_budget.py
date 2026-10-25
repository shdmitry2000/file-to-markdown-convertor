"""Warm children are stopped between jobs when they hold more memory than the pod can spare.

Both warm children (docling, pre-warmed at start, and dbank, from its first job)
live as long as the worker pod, and each keeps the memory of the largest document
it has converted. On the sandbox the two sat at ~3.7 GB in a 4.5 GB worker and the
next 85-page PDF OOM-killed the pod — the job in flight was lost and every ingest
waited for the restart. The budget check runs after every warm job: the idle other
child goes first, then the one that just ran.
"""

from types import SimpleNamespace

import pytest

from app.workers import worker as w


class _Child:
    """Stands in for a warm worker: a pid-bearing process and a _kill() that records."""

    def __init__(self, name, rss):
        self.name, self.rss, self.killed = name, rss, False
        self._proc = SimpleNamespace(pid=hash(name) & 0xFFFF)

    def _kill(self):
        self.killed, self._proc = True, None


@pytest.fixture
def children(monkeypatch):
    docling, dbank = _Child("docling", 0), _Child("dbank", 0)
    monkeypatch.setattr(w, "_WARM_DOCLING", docling)
    monkeypatch.setattr(w, "_WARM_DBANK", dbank)
    by_pid = {docling._proc.pid: docling, dbank._proc.pid: dbank}
    monkeypatch.setattr(w, "_rss_mb", lambda proc: by_pid[proc.pid].rss)
    return docling, dbank


def test_under_budget_nothing_is_stopped(children, monkeypatch):
    docling, dbank = children
    monkeypatch.setenv("WARM_MEMORY_BUDGET_MB", "3000")
    docling.rss, dbank.rss = 1500, 1200
    w._enforce_warm_budget(docling)
    assert not docling.killed and not dbank.killed


def test_over_budget_the_idle_other_child_goes_first(children, monkeypatch):
    docling, dbank = children
    monkeypatch.setenv("WARM_MEMORY_BUDGET_MB", "3000")
    docling.rss, dbank.rss = 2500, 1300          # the sandbox shape: 3.8 GB of 4.3
    w._enforce_warm_budget(docling)
    assert dbank.killed and not docling.killed, "the child that just ran was stopped first"


def test_the_child_that_ran_goes_too_when_it_alone_is_over(children, monkeypatch):
    docling, dbank = children
    monkeypatch.setenv("WARM_MEMORY_BUDGET_MB", "3000")
    docling.rss, dbank.rss = 3400, 500
    w._enforce_warm_budget(docling)
    assert dbank.killed and docling.killed


def test_a_child_that_is_not_running_is_skipped(children, monkeypatch):
    docling, dbank = children
    monkeypatch.setenv("WARM_MEMORY_BUDGET_MB", "1000")
    dbank._proc = None
    docling.rss = 1200
    w._enforce_warm_budget(docling)
    assert docling.killed and not dbank.killed


def test_zero_turns_the_budget_off(children, monkeypatch):
    docling, dbank = children
    monkeypatch.setenv("WARM_MEMORY_BUDGET_MB", "0")
    docling.rss, dbank.rss = 9000, 9000
    w._enforce_warm_budget(docling)
    assert not docling.killed and not dbank.killed


def test_a_failing_check_never_fails_the_job(children, monkeypatch):
    docling, _ = children
    monkeypatch.setenv("WARM_MEMORY_BUDGET_MB", "100")
    monkeypatch.setattr(w, "_rss_mb", lambda proc: 1 / 0)
    w._enforce_warm_budget(docling)   # must not raise


def test_default_budget_is_70_percent_of_the_container_limit(monkeypatch):
    monkeypatch.delenv("WARM_MEMORY_BUDGET_MB", raising=False)
    monkeypatch.setattr(w, "_cgroup_memory_limit_mb", lambda: 4291.5)
    assert w._warm_memory_budget_mb() == pytest.approx(3004.05)
    monkeypatch.setattr(w, "_cgroup_memory_limit_mb", lambda: None)
    assert w._warm_memory_budget_mb() is None, "no limit (a laptop): no budget"


@pytest.mark.parametrize("files, expected", [
    ({"/sys/fs/cgroup/memory.max": "4500000000\n"}, 4500000000 / 1048576),       # v2
    ({"/sys/fs/cgroup/memory.max": "max\n"}, None),                               # v2 unlimited
    ({"/sys/fs/cgroup/memory/memory.limit_in_bytes": "4500000000\n"}, 4500000000 / 1048576),  # v1
    ({"/sys/fs/cgroup/memory/memory.limit_in_bytes": str(2**63 - 4096)}, None),   # v1 unlimited
    ({}, None),                                                                   # no cgroup
])
def test_cgroup_limit_is_read_from_v2_then_v1(monkeypatch, files, expected):
    class _P:
        def __init__(self, path):
            self.path = path

        def read_text(self):
            if self.path not in files:
                raise OSError(self.path)
            return files[self.path]

    monkeypatch.setattr(w, "Path", _P)
    assert w._cgroup_memory_limit_mb() == (pytest.approx(expected) if expected else None)


@pytest.mark.parametrize("lane, attr", [("docling", "_WARM_DOCLING"), ("dbank", "_WARM_DBANK")])
@pytest.mark.parametrize("fails", [False, True])
def test_the_warm_route_checks_the_budget_after_every_job(monkeypatch, tmp_path, lane, attr, fails):
    """Success or failure, the check runs once the warm job is over, for that child."""
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 minimal")
    seen = []
    monkeypatch.setattr(w, "_enforce_warm_budget", seen.append)
    monkeypatch.setattr(w, "_warm_enabled", lambda: True)
    monkeypatch.setattr(w, "_slide_deck_reason", lambda path: None)
    child = getattr(w, attr)

    def convert(*args, **kwargs):
        if fails:
            raise RuntimeError("conversion failed")
        return {"markdown": "# ok"}

    monkeypatch.setattr(child, "convert", convert)
    if fails:
        with pytest.raises(RuntimeError):
            w._run_converter_with_timeout(str(pdf), "c1", lane, 10)
    else:
        assert w._run_converter_with_timeout(str(pdf), "c1", lane, 10) == {"markdown": "# ok"}
    assert seen == [child]
