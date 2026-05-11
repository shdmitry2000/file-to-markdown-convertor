# Testing the Markdown Worker

## Quick Start

```bash
cd file-to-markdown-convertor

pip install -r tests/requirements-test.txt

# Run everything
pytest tests/ -v

# Just the timeout suite
./run_timeout_tests.sh
```

The current full suite: **37 tests**, ~15 s.

## Test files

| File | Purpose |
|------|---------|
| `tests/test_timeout_mechanism.py` | Multiprocessing timeout wrapper — see [tests/README_TIMEOUT_TESTS.md](tests/README_TIMEOUT_TESTS.md) for details. |
| `tests/_timeout_targets.py` | Module-level subprocess targets used by the timeout tests (must be picklable for `spawn`). |
| `tests/test_worker.py` | Worker-level routing, environment detection, output paths, metadata. |
| `tests/test_markdown_api.py` | FastAPI endpoints (`/convert`, `/converted/{path}`, `/capabilities`, `/health`). |
| `tests/test_converters.py` | Per-converter integration tests. |
| `tests/conftest.py` | Shared fixtures (`sample_pdf`, `converted_files_dir`, etc.). |

## Selective runs

```bash
# By file
pytest tests/test_timeout_mechanism.py -v
pytest tests/test_worker.py -v

# By substring
pytest tests/ -k "timeout" -v
pytest tests/ -k "ocr" -v

# Single test
pytest tests/test_timeout_mechanism.py::TestTimeoutMechanism::test_large_payload_does_not_spuriously_timeout -v
```

## Coverage

```bash
pytest tests/ --cov=app --cov-report=html
open htmlcov/index.html
```

## Manual smoke test against a running worker

```bash
# Start the API + worker (locally or in docker-compose)
export DOCLING_TIMEOUT_SECONDS=60   # short timeout for testing
python -m app.workers.worker &
uvicorn app.api.main:app --port 8000 &

# Submit a conversion
curl -X POST http://localhost:8000/convert \
  -H 'Content-Type: application/json' \
  -d '{"file_path": "/abs/path/to/sample.pdf", "converter_type": "docling"}'

# Poll
ID=...   # from previous response
watch -n 1 "curl -s http://localhost:8000/convert/$ID"

# Fetch the result
curl -s "http://localhost:8000/converted/$(basename /abs/path/to/sample.pdf)"
```

## CI snippet

```yaml
- name: Install
  run: |
    cd file-to-markdown-convertor
    pip install -e .
    pip install -r tests/requirements-test.txt

- name: Test
  run: |
    cd file-to-markdown-convertor
    pytest tests/ -v --cov=app --cov-report=xml
```

## Troubleshooting

- **Tests hang.** A previous run may have leaked subprocesses:
  `pkill -f test_timeout` then re-run.
- **`AttributeError: ... has no attribute 'DocumentConverter'`** when patching.
  The worker no longer imports docling at module top level (it's loaded inside
  the spawned subprocess). Patch `app.workers.worker._run_converter_with_timeout`
  instead, or pass a `_target=` callable.
- **Memory test failures** under load: `pytest tests/ -k "not memory"`.
