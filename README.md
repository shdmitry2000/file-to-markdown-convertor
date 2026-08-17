# File to Markdown Converter

A distributed file conversion service that converts PDF and other documents to Markdown using [Docling](https://github.com/DS4SD/docling). Uses ZeroMQ for task distribution and FastAPI for the REST API.

## Architecture

```
┌─────────────┐      ZeroMQ       ┌─────────────┐
│   FastAPI   │ ──────────────▶  │   Worker    │
│     API     │  tcp://:5555     │   (PULL)    │
│   (PUSH)    │                  │             │
│             │ ◀──────────────  │   Docling   │
│  Status DB  │  tcp://:5556     │ Converter   │
└─────────────┘                  └─────────────┘
```

- **API Server**: Receives conversion requests, queues tasks, returns status
- **Workers**: Pull tasks, convert files using Docling, push results back
- **ZeroMQ**: Message queue for task distribution and result collection

### Kubernetes / ZeroMQ (recommended layout)

Split-brain or silent hangs usually come from **(a)** routing conversion HTTP and ZeroMQ to **different API replicas**, **(b)** kube-proxy long-lived TCP oddities, or **(c)** workers reconnecting while PUSH queued tasks behave oddly.

**Robust pattern (matches this repo’s `docker-compose.yml` idea — API + worker in one lifecycle unit):**

1. Run **`markdown-worker` as a second container in the same Pod as `markdown-api`** (sidecar).
2. Bind/listen addresses unchanged (`tcp://*:5555` / `*:5556` on the API container).
3. Point the worker at loopback only:

   - `MARKDOWN_ZMQ_PEER_HOST=127.0.0.1` **or**
   - `ZMQ_HOST=127.0.0.1` / `ZEROMQ_HOST=127.0.0.1`

Then ZeroMQ never crosses ClusterIP or multiple replicas.

**If you keep separate Deployments instead:**

- **`markdown-api` replicas must stay `1`** until conversion status moves off in-memory maps into Redis/shared storage.
- Expose ZMQ ports on the Service **only if workers are separate Pods**; after Helm edits restart API **then** workers (clean handshake).

Environment notes:

- Kubernetes defaults assume **`markdown-api`** as the DNS name (`docker-compose` may still use `api`; override with `ZMQ_HOST` if needed).
- Helm keys **`ZMQ_HOST`** are honored (`ZEROMQ_HOST` is an alias).

## Features

- ✅ Automatic Docker/Standalone mode detection
- ✅ Scalable worker pool (4 workers default in Docker)
- ✅ Async status tracking
- ✅ Frontmatter metadata in converted files
- ✅ Health check endpoint
- ✅ Preserves directory structure
- ✅ Formula-aware Excel conversion (see below)

## Excel: formulas, not just numbers

`.xlsx` / `.xlsm` route to the `excel` converter (`app/converters/excel/`), which
keeps what a spreadsheet means rather than only what it shows. markitdown and
docling both read Excel values-only, so a totals cell arrives as a bare number
with nothing tying it to the rows behind it.

Each sheet renders as one markdown table **per region** (several blocks on one
sheet stay separate), followed by:

```markdown
### Formulas / נוסחאות — הלוואות
- **C8** · סך יתרות ההלוואות בכל הבנקים (₪) = 2,736,000
  - `=SUM(C3:C7)` = הפועלים · משכנתא · יתרה (₪)=850,000 + הפועלים · צרכנית · יתרה (₪)=120,000 + …

### Dependencies / תלויות — הלוואות
- הפועלים · משכנתא · יתרה (₪) (C3) → feeds ... (הלוואות!C8)
```

Cell names come from metadata the workbook already carries — comments, defined
names, Table headers, freeze panes, then header/label inference — so **no LLM is
called**. Nothing is recalculated either: a formula whose result Excel never
saved is reported as `(no cached result)`, never computed and never guessed.

Legacy `.xls` stays on markitdown (openpyxl cannot read it; xlrd exposes no
formulas).

Tuning — all optional:

| Variable | Default | Effect |
|---|---|---|
| `EXCEL_MAX_CELLS` | 200000 | Whole-workbook read budget; sheets that hit it are marked truncated |
| `EXCEL_MAX_REFS_PER_FORMULA` | 20 | Terms expanded per formula before `+ …` |
| `EXCEL_EXPAND_DEPTH` | 1 | Levels of formula-into-formula expansion |
| `EXCEL_EMIT_DEPENDENCIES` | true | Set `false` to drop the dependency section |

Fixtures live in `tests/fixtures/excel/` — see the README there to rebuild them.

## Quick Start

### Docker Mode (Recommended for Production)

```bash
# Start both API and workers
docker compose up

# Scale workers
docker compose up --scale worker=8

# Stop all services
docker compose down
```

### Standalone Mode (Development/Testing)

**Terminal 1** - Start API:
```bash
uvicorn app.api.main:app --reload
```

**Terminal 2** - Start Worker(s):
```bash
# Option 1: Use helper script
./run_worker_standalone.sh

# Option 2: Run directly
python -m app.workers.worker

# Option 3: Specify custom host
python -m app.workers.worker --host localhost
```

## Usage

### 1. Submit Conversion Request

```bash
curl -X POST http://localhost:8000/convert \
  -H "Content-Type: application/json" \
  -d '{"file_path": "files_to_convert/document.pdf"}'
```

Response:
```json
{
  "conversion_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

### 2. Check Status

```bash
curl http://localhost:8000/convert/550e8400-e29b-41d4-a716-446655440000
```

Response:
```json
{
  "status": "completed"  // or "pending", "processing", "failed", "cancelled"
}
```

### 3. Cancel a Conversion

```bash
curl -X DELETE http://localhost:8000/convert/550e8400-e29b-41d4-a716-446655440000
```

Stops the work, not just the waiting. A job that has **not started** is dropped
before it reaches the converter; a job already **converting** has its converter
subprocess killed so the slot returns to the pool. Either way the conversion ends
as `cancelled` — which is not `failed`, because nothing is wrong with the
document.

Two behaviours worth knowing before relying on it:

- **Conversions are shared between callers.** Two callers converting the same file
  get the same `conversion_id`, so a cancel means "I have stopped waiting"; the
  work only stops when the *last* caller has cancelled. Until then the response
  reports the conversion's real status rather than `cancelled`.
- **Killing an active conversion discards the warm model.** The next job on that
  worker pays a cold start (~10-12s). Worth it for a long conversion, a loss for a
  short one.

Cancellation reaches the workers through a marker file in `CANCEL_MARKER_DIR`
(default: alongside the converted files). **The API and the workers must share
that directory** — they already must, to exchange documents at all, but a
deployment that separates them breaks cancellation silently.

### 4. Retrieve Converted File

```bash
curl http://localhost:8000/converted/document.md
```

### 5. Health Check

```bash
curl http://localhost:8000/health
```

## File Structure

```
file-to-markdown-convertor/
├── app/
│   ├── api/
│   │   └── main.py           # FastAPI application
│   └── workers/
│       └── worker.py         # ZeroMQ worker
├── tests/
│   ├── files_to_convert/     # Input files (mounted in Docker)
│   └── converted_files/      # Output files (mounted in Docker)
├── docker-compose.yml        # Docker orchestration
├── Dockerfile               # Container image
├── run_worker_standalone.sh # Standalone worker launcher
└── test_worker.sh          # Integration test script
```

## Environment Detection

The worker automatically detects its environment:

| Detection Method | Docker | Standalone |
|-----------------|--------|------------|
| `DOCKER_CONTAINER=true` env var | ✅ | ❌ |
| `/.dockerenv` file exists | ✅ | ❌ |
| Default ZeroMQ host | `api` | `localhost` |

Override with `--host` argument:
```bash
python -m app.workers.worker --host custom-host
```

## Testing

Run the comprehensive test:
```bash
./test_worker.sh
```

This checks:
- ✅ API health
- ✅ Worker process
- ✅ File conversion flow
- ✅ Status tracking

## Troubleshooting

### Worker stuck on "processing"

**Symptom**: Status stays at "processing" indefinitely

**Causes**:
1. Worker not running
2. Wrong ZeroMQ host
3. Worker crashed during conversion

**Solutions**:
```bash
# Check if worker is running
pgrep -f "workers/worker.py"

# Check worker logs for connection info
# Should see: "Connecting to ZeroMQ host: localhost (Docker mode: False)"

# Restart worker
./run_worker_standalone.sh
```

### Connection refused errors

**Symptom**: `zmq.error.ZMQError: Connection refused`

**Cause**: API not running or wrong port

**Solution**:
```bash
# Verify API is running
curl http://localhost:8000/health

# Start API if needed
uvicorn app.api.main:app --reload
```

### File not found errors

**Symptom**: `404 File not found`

**Solution**:
- Place files in `files_to_convert/` directory
- Use relative path from that directory
- Example: `files_to_convert/docs/report.pdf` → `"file_path": "docs/report.pdf"`

## Configuration

### Worker Replicas (Docker)

Edit `docker-compose.yml`:
```yaml
worker:
  deploy:
    replicas: 8  # Increase for more parallelism
```

### ZeroMQ Ports

Default ports (in `app/api/main.py`):
- Tasks: `5555` (PUSH from API, PULL by workers)
- Results: `5556` (PUSH from workers, PULL by API)

### Cancellation

| Variable | Default | Effect |
|---|---|---|
| `CANCEL_MARKER_DIR` | `{CONVERTED_FILES_DIR}/.cancelled` | Where cancel markers are written and read. **Must be visible to both the API and the workers.** |
| `CANCEL_MARKER_TTL_SECONDS` | `DOCLING_TIMEOUT_SECONDS` + 1h | Age past which a marker has no job left to stop and is swept |
| `CANCEL_MARKER_SWEEP_INTERVAL_SECONDS` | `3600` | How often the API sweeps stale markers |

Why a file rather than a message: a worker mid-conversion is blocked inside its
job and is **not reading its ZeroMQ socket**, so a cancel sent on the task queue
would be delivered to an idle worker or wait behind the busy one until it
finished — exactly when it is worthless.

Terminal statuses clear their own marker. The sweeper exists for the ones that
never reach a terminal status: a cancel for a job that was never dispatched, or a
restart landing between the cancel and the worker reporting back.

### Output Structure

Converted files maintain directory structure:
```
files_to_convert/
  project1/
    doc.pdf
    
converted_files/
  project1/
    doc.md        # With frontmatter metadata
```

## Metadata Format

Each converted file includes frontmatter:

```markdown
---
source_file: files_to_convert/project1/doc.pdf
conversion_id: 550e8400-e29b-41d4-a716-446655440000
conversion_date: '2026-03-03T10:30:00.123456'
docling_name: doc.pdf
docling_origin: DocumentOrigin.PDF
docling_num_pages: 42
---

# Document Content

Converted markdown here...
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/convert` | POST | Submit file for conversion |
| `/convert/{id}` | GET | Check conversion status |
| `/convert/{id}` | DELETE | Cancel a conversion — drops it if queued, kills its converter if running |
| `/converted/{path}` | GET | Retrieve converted file |
| `/debug/queue` | GET | Pending and active conversions |
| `/health` | GET | Service health check |

## Dependencies

- **FastAPI**: Web framework
- **ZeroMQ (pyzmq)**: Message queue
- **Docling**: Document conversion
- **python-frontmatter**: Metadata headers
- **Uvicorn**: ASGI server

## License

See main project LICENSE.

## See Also

- [WORKER_FIX.md](./docs/archive/WORKER_FIX.md) - Details on Docker/standalone fix (archived)
- [Docling Documentation](https://github.com/DS4SD/docling)
- [ZeroMQ Guide](https://zeromq.org/get-started/)
