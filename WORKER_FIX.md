# Markdown Worker Docker/Standalone Mode Fix

## Problem
The worker was stuck on "prepare markdown" when running in standalone mode because it was trying to connect to the ZeroMQ host `api` (Docker service name) instead of `localhost`.

## Solution
The worker now auto-detects whether it's running in Docker or standalone mode:

### Auto-Detection Logic
The worker checks for Docker environment in this order:
1. If `--host` argument is provided, use that explicitly
2. Otherwise, check if `DOCKER_CONTAINER=true` environment variable is set
3. Otherwise, check if `/.dockerenv` file exists (Docker creates this)
4. If in Docker → use `api` as host
5. If standalone → use `localhost` as host

### Changes Made

#### 1. Worker Code (`app/workers/worker.py`)
- Changed default host from hardcoded `"api"` to auto-detection
- Added environment variable check: `DOCKER_CONTAINER`
- Added Docker file check: `/.dockerenv`
- Logs the detected mode for debugging

#### 2. Docker Compose (`docker-compose.yml`)
- Added `DOCKER_CONTAINER=true` environment variable to worker service
- Removed explicit `--host api` from command (now auto-detected)

#### 3. Standalone Runner Script (`run_worker_standalone.sh`)
- New script to easily run worker in standalone mode
- Automatically uses localhost connections

## Usage

### Docker Mode (recommended for production)
```bash
docker compose up
```
The worker will automatically detect Docker mode and connect to `api:5555` and `api:5556`.

### Standalone Mode (for development/testing)
```bash
# Option 1: Use the helper script
./run_worker_standalone.sh

# Option 2: Run directly
python -m app.workers.worker

# Option 3: Specify host explicitly
python -m app.workers.worker --host localhost
```

### Verification
Check the logs for:
```
Connecting to ZeroMQ host: localhost (Docker mode: False)  # standalone
Connecting to ZeroMQ host: api (Docker mode: True)          # Docker
```

## File Status Tracking
The markdown conversion process now properly handles:
- `pending` - Task queued but not yet picked up by worker
- `processing` - Worker actively converting file
- `completed` - Conversion successful
- `failed` - Conversion error

## Testing
To test the fix with your stuck file (439.pdf):

1. **Start API server** (in terminal 1):
   ```bash
   cd file-to-markdown-convertor
   uvicorn app.api.main:app --reload
   ```

2. **Start worker** (in terminal 2):
   ```bash
   cd file-to-markdown-convertor
   ./run_worker_standalone.sh
   ```

3. **Submit conversion request**:
   ```bash
   curl -X POST http://localhost:8000/convert \
     -H "Content-Type: application/json" \
     -d '{"file_path": "files_to_convert/439.pdf"}'
   ```

4. **Check status** (use conversion_id from response):
   ```bash
   curl http://localhost:8000/convert/{conversion_id}
   ```

## Benefits
- ✅ Works in both Docker and standalone environments
- ✅ No manual configuration needed
- ✅ Backward compatible with existing Docker deployments
- ✅ Easy to override with `--host` flag if needed
- ✅ Clear logging shows detected mode
