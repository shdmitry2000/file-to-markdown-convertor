"""Tests for the multiprocessing timeout wrapper.

These tests inject a module-level subprocess target via the `_target` hook on
`_run_converter_with_timeout`, so they exercise the real spawn/join/terminate
logic without depending on docling or any heavy converter being installed.
Mocking `app.workers.worker.DocumentConverter` would not survive the spawn
start method — patches in the parent process are not inherited.
"""

import multiprocessing
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import psutil
import pytest

from app.workers.worker import (
    _run_converter_with_timeout,
    convert_file_to_markdown,
)
from tests import _timeout_targets as targets


@pytest.fixture(autouse=True, scope="module")
def _force_spawn():
    """Match the production start method so tests catch spawn-only bugs."""
    try:
        multiprocessing.set_start_method("spawn", force=True)
    except RuntimeError:
        pass


@pytest.fixture
def sample_pdf_path(tmp_path):
    """Path to a placeholder file. Targets don't read it, so contents don't matter."""
    p = tmp_path / "test.pdf"
    p.write_bytes(b"%PDF-1.4\n%fake\n")
    return p


class TestTimeoutMechanism:
    def test_success(self, sample_pdf_path):
        result = _run_converter_with_timeout(
            file_path=str(sample_pdf_path),
            conversion_id="t-success",
            converter_type="docling",
            timeout_seconds=10,
            _target=targets.succeed_immediately,
        )
        assert result["markdown"] == "# fake"
        assert result["num_pages"] == 1
        assert result["converter_used"] == "docling"

    def test_large_payload_does_not_spuriously_timeout(self, sample_pdf_path):
        """Regression: a multi-MB put used to deadlock when join() ran first."""
        result = _run_converter_with_timeout(
            file_path=str(sample_pdf_path),
            conversion_id="t-large",
            converter_type="docling",
            timeout_seconds=15,
            _target=targets.succeed_with_large_payload,
        )
        assert len(result["markdown"]) == 5 * 1024 * 1024

    def test_timeout_terminates_runaway(self, sample_pdf_path):
        start = time.time()
        with pytest.raises(TimeoutError) as exc_info:
            _run_converter_with_timeout(
                file_path=str(sample_pdf_path),
                conversion_id="t-timeout",
                converter_type="docling",
                timeout_seconds=2,
                _target=targets.sleep_forever,
            )
        elapsed = time.time() - start
        assert elapsed < 8, f"timeout path took {elapsed:.1f}s"
        assert "timeout" in str(exc_info.value).lower()

    def test_subprocess_error_propagates(self, sample_pdf_path):
        with pytest.raises(Exception) as exc_info:
            _run_converter_with_timeout(
                file_path=str(sample_pdf_path),
                conversion_id="t-error",
                converter_type="docling",
                timeout_seconds=10,
                _target=targets.raise_value_error,
            )
        assert "Intentional converter failure" in str(exc_info.value)

    def test_silent_crash_reports_exit_code(self, sample_pdf_path):
        with pytest.raises(Exception) as exc_info:
            _run_converter_with_timeout(
                file_path=str(sample_pdf_path),
                conversion_id="t-crash",
                converter_type="docling",
                timeout_seconds=10,
                _target=targets.crash_silently,
            )
        msg = str(exc_info.value)
        assert "exit" in msg.lower()
        assert "7" in msg  # the os._exit code we used

    def test_no_zombie_processes_after_timeout(self, sample_pdf_path):
        parent = psutil.Process(os.getpid())
        baseline = len(parent.children(recursive=True))

        with pytest.raises(TimeoutError):
            _run_converter_with_timeout(
                file_path=str(sample_pdf_path),
                conversion_id="t-zombies",
                converter_type="docling",
                timeout_seconds=1,
                _target=targets.sleep_forever,
            )

        time.sleep(0.5)
        assert len(parent.children(recursive=True)) == baseline

    def test_sigkill_fallback_when_sigterm_ignored(self, sample_pdf_path):
        """Process that ignores SIGTERM should still be killed within ~grace+timeout."""
        start = time.time()
        with pytest.raises(TimeoutError):
            _run_converter_with_timeout(
                file_path=str(sample_pdf_path),
                conversion_id="t-sigkill",
                converter_type="docling",
                timeout_seconds=2,
                _target=targets.ignore_sigterm_then_sleep,
            )
        elapsed = time.time() - start
        assert elapsed < 12, f"SIGKILL fallback took {elapsed:.1f}s"

    def test_env_timeout_is_respected(self, sample_pdf_path, monkeypatch):
        """convert_file_to_markdown must honour DOCLING_TIMEOUT_SECONDS."""
        monkeypatch.setenv("DOCLING_TIMEOUT_SECONDS", "2")

        mock_socket = MagicMock()
        with patch(
            "app.workers.worker._run_converter_with_timeout",
            side_effect=TimeoutError("Conversion exceeded 2s timeout and was terminated"),
        ) as mock_wrapper:
            convert_file_to_markdown(
                str(sample_pdf_path),
                "t-env",
                mock_socket,
                converter_type="pymupdf",
            )
        assert mock_wrapper.call_args.kwargs["timeout_seconds"] == 2

        sent = [c.args[0] for c in mock_socket.send_json.call_args_list]
        assert any(c.get("status") == "failed" and "timeout" in c.get("error", "").lower() for c in sent)


class TestRoutingIntegration:
    """convert_file_to_markdown delegates to _run_converter_with_timeout."""

    def test_docling_path_uses_wrapper_with_explicit_ocr(self, sample_pdf_path, monkeypatch):
        monkeypatch.delenv("DOCLING_DO_OCR", raising=False)  # default: off
        mock_socket = MagicMock()

        with patch("app.workers.worker._run_converter_with_timeout") as mock_wrapper:
            mock_wrapper.return_value = {
                "markdown": "# Test",
                "num_pages": 1,
                "doc_name": "test.pdf",
                "doc_origin": "PDF",
            }
            convert_file_to_markdown(
                str(sample_pdf_path),
                "t-docling",
                mock_socket,
                converter_type="docling",
            )

        assert mock_wrapper.called
        kwargs = mock_wrapper.call_args.kwargs
        assert kwargs["converter_type"] == "docling"
        assert kwargs["timeout_seconds"] == 7200  # 2h ceiling by default (plus the stall cut)
        assert kwargs["do_ocr"] is False  # default off, passed explicitly

    def test_docling_path_forwards_ocr_when_env_set(self, sample_pdf_path, monkeypatch):
        monkeypatch.setenv("DOCLING_DO_OCR", "true")
        mock_socket = MagicMock()

        with patch("app.workers.worker._run_converter_with_timeout") as mock_wrapper:
            mock_wrapper.return_value = {
                "markdown": "# Test",
                "num_pages": 1,
                "doc_name": "test.pdf",
                "doc_origin": "PDF",
            }
            convert_file_to_markdown(
                str(sample_pdf_path),
                "t-docling-ocr",
                mock_socket,
                converter_type="docling",
            )

        assert mock_wrapper.call_args.kwargs["do_ocr"] is True

    def test_plugin_path_uses_wrapper(self, sample_pdf_path):
        mock_socket = MagicMock()
        with patch("app.workers.worker._run_converter_with_timeout") as mock_wrapper:
            mock_wrapper.return_value = {
                "markdown": "# pymupdf",
                "num_pages": 1,
                "doc_name": "test.pdf",
                "doc_origin": "pymupdf",
            }
            convert_file_to_markdown(
                str(sample_pdf_path),
                "t-pymupdf",
                mock_socket,
                converter_type="pymupdf",
            )
        assert mock_wrapper.called
        assert mock_wrapper.call_args.kwargs["converter_type"] == "pymupdf"

    def test_timeout_error_sends_failed_status(self, sample_pdf_path):
        mock_socket = MagicMock()
        with patch(
            "app.workers.worker._run_converter_with_timeout",
            side_effect=TimeoutError("Conversion exceeded 10s timeout"),
        ):
            convert_file_to_markdown(
                str(sample_pdf_path),
                "t-fail-status",
                mock_socket,
                converter_type="pymupdf",
            )

        sent = [c.args[0] for c in mock_socket.send_json.call_args_list]
        failed = [c for c in sent if c.get("status") == "failed"]
        assert failed
        assert any("timeout" in c.get("error", "").lower() for c in failed)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])


class TestStallNotClock:
    """A long conversion is not a stuck one: only no-progress ends it."""

    def test_busy_child_runs_past_the_old_ceiling(self, monkeypatch):
        import multiprocessing as mp
        import time as _t
        from app.workers.worker import _ProgressWatch

        monkeypatch.setenv("DOCLING_STALL_SECONDS", "2")
        p = mp.get_context("spawn").Process(target=_burn, args=(4.0,))
        p.start()
        try:
            w = _ProgressWatch(p.pid, 0)            # no ceiling
            end = _t.monotonic() + 3.5
            while _t.monotonic() < end:
                _t.sleep(0.5)
                assert w.check() is None             # busy → never cut
        finally:
            p.join()

    def test_idle_child_is_cut_as_stalled(self, monkeypatch):
        import multiprocessing as mp
        import time as _t
        from app.workers.worker import _ProgressWatch

        monkeypatch.setenv("DOCLING_STALL_SECONDS", "1.5")
        p = mp.get_context("spawn").Process(target=_t.sleep, args=(10,))
        p.start()
        try:
            _t.sleep(1.0)                            # let the interpreter finish starting
            w = _ProgressWatch(p.pid, 0)
            verdict = None
            end = _t.monotonic() + 5
            while verdict is None and _t.monotonic() < end:
                _t.sleep(0.5)
                verdict = w.check()
            assert verdict == "stalled"
        finally:
            p.terminate(); p.join()

    def test_optional_ceiling_still_applies(self):
        import os
        from app.workers.worker import _ProgressWatch

        w = _ProgressWatch(os.getpid(), 1)
        import time as _t
        _t.sleep(1.1)
        assert w.check() == "timeout"


def _burn(seconds):
    import time as _t
    end = _t.monotonic() + seconds
    x = 0
    while _t.monotonic() < end:
        x += 1


class TestPageProgress:
    """With docling's page counter the watch trusts pages moving, not CPU."""

    def test_pages_moving_is_progress(self, monkeypatch):
        import multiprocessing as mp, time as _t
        from app.workers.worker import _ProgressWatch

        monkeypatch.setenv("DOCLING_STALL_SECONDS", "1")
        counter = mp.get_context("spawn").Value("q", 0)
        w = _ProgressWatch(None, 0, progress=counter)
        for _ in range(4):
            _t.sleep(0.4)
            counter.value += 6          # a page went through the six stages
            assert w.check() is None

    def test_busy_but_no_pages_is_stalled(self, monkeypatch):
        """CPU may be pegged, but if no page moves the conversion is stuck."""
        import multiprocessing as mp, os, time as _t
        from app.workers.worker import _ProgressWatch

        monkeypatch.setenv("DOCLING_STALL_SECONDS", "1")
        counter = mp.get_context("spawn").Value("q", 5)
        w = _ProgressWatch(os.getpid(), 0, progress=counter)   # this process is busy
        _t.sleep(1.2)
        assert w.check() == "stalled"


def test_page_hook_counts_every_stage_and_finished_pages():
    import multiprocessing as mp
    from docling.pipeline import standard_pdf_pipeline as spp
    from app.workers.worker import _install_page_progress

    original = spp.ThreadedPipelineStage._emit
    try:
        ctx = mp.get_context("spawn")
        events, pages = ctx.Value("q", 0), ctx.Value("q", 0)
        _install_page_progress(events, pages)

        class _Stage:
            def __init__(self, name):
                self.name, self._outputs, self._postprocess = name, [], None
        spp.ThreadedPipelineStage._emit(_Stage("layout"), [1, 2])
        spp.ThreadedPipelineStage._emit(_Stage("assemble"), [1])
        assert events.value == 3 and pages.value == 1
    finally:
        spp.ThreadedPipelineStage._emit = original
