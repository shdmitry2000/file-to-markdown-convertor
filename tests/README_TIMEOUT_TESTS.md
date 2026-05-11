# Timeout Mechanism Tests

Tests for the multiprocessing timeout wrapper in `app/workers/worker.py` that
protects all converter types (docling, pymupdf, markitdown, marker, vlm) from
runaway conversions.

## Files

- **`test_timeout_mechanism.py`** — exercises `_run_converter_with_timeout`
  end-to-end via the `_target=` injection hook, so tests run real spawn/join/
  terminate logic without depending on docling.
- **`_timeout_targets.py`** — module-level subprocess targets (succeed, sleep,
  crash, ignore-SIGTERM, large-payload). Module-level so they survive `spawn`
  pickling.

## Why we don't mock `app.workers.worker.DocumentConverter`

The worker uses `multiprocessing.set_start_method('spawn', force=True)`. Under
`spawn`, child processes start a fresh interpreter and re-import every module —
patches applied in the parent process are **not** inherited. The previous test
suite happened to work on Linux (default `fork`) but tested nothing on macOS.

The new tests pass a module-level `_target` callable into
`_run_converter_with_timeout`; that callable is pickled and re-executed inside
the spawned subprocess, exactly like the real `_convert_in_subprocess`.

## Running

```bash
cd file-to-markdown-convertor

# All timeout tests
pytest tests/test_timeout_mechanism.py -v

# Single class / test
pytest tests/test_timeout_mechanism.py::TestTimeoutMechanism -v
pytest tests/test_timeout_mechanism.py::TestTimeoutMechanism::test_large_payload_does_not_spuriously_timeout -v

# With coverage
pytest tests/test_timeout_mechanism.py --cov=app.workers.worker --cov-report=html
```

Or use the helper:

```bash
./run_timeout_tests.sh
./run_timeout_tests.sh -v
./run_timeout_tests.sh --coverage
```

## What is covered

`TestTimeoutMechanism` (8 tests):

| Test | What it proves |
|------|----------------|
| `test_success` | Happy path: result is returned and child exits cleanly. |
| `test_large_payload_does_not_spuriously_timeout` | Regression: a 5 MB result does not deadlock the queue/join ordering. |
| `test_timeout_terminates_runaway` | A subprocess that never returns is killed within `timeout_seconds`. |
| `test_subprocess_error_propagates` | An exception inside the subprocess is reported on the queue and re-raised in the parent. |
| `test_silent_crash_reports_exit_code` | A subprocess that exits non-zero without producing a result surfaces the exit code in the error. |
| `test_no_zombie_processes_after_timeout` | After a timeout, the parent's child count returns to baseline. |
| `test_sigkill_fallback_when_sigterm_ignored` | A subprocess that ignores SIGTERM is still killed within ~ timeout + grace. |
| `test_env_timeout_is_respected` | `convert_file_to_markdown` reads `DOCLING_TIMEOUT_SECONDS` and forwards it. |

`TestRoutingIntegration` (4 tests):

| Test | What it proves |
|------|----------------|
| `test_docling_path_uses_wrapper_with_explicit_ocr` | Docling path forwards `do_ocr=False` (default) explicitly — no env lookup inside the wrapper. |
| `test_docling_path_forwards_ocr_when_env_set` | `DOCLING_DO_OCR=true` is read once in the parent and passed through. |
| `test_plugin_path_uses_wrapper` | Plugin converters (pymupdf etc.) go through the same timeout wrapper. |
| `test_timeout_error_sends_failed_status` | A `TimeoutError` from the wrapper produces a `failed` status with a "timeout" error message. |

Total: **12 tests**, ~13 s on an M-series Mac.

## Troubleshooting

**Tests hang.** Find stuck children with `pgrep -P $$ -a` and kill with
`pkill -f test_timeout`.

**Subprocess error tests fail with "module has no attribute X".** You probably
patched `app.workers.worker.<symbol>` — patches don't cross the spawn boundary.
Use the `_target=` hook instead, or move the symbol into `_timeout_targets.py`.
