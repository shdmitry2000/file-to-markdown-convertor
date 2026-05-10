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
  "status": "completed"  // or "pending", "processing", "failed"
}
```

### 3. Retrieve Converted File

```bash
curl http://localhost:8000/converted/document.md
```

### 4. Health Check

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
| `/converted/{path}` | GET | Retrieve converted file |
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
