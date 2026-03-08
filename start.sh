#!/bin/bash
# Start script for markdown-api service
# Runs both the API server and the worker process

set -e

echo "Starting Markdown API and Worker..."

# Set environment variable to indicate Docker mode
export DOCKER_CONTAINER=true
export RUNNING_IN_DOCKER=true

# Start the worker in the background
python -m workers.worker &
WORKER_PID=$!
echo "Worker started with PID: $WORKER_PID"

# Start the API server in the foreground
uvicorn api.main:app --host 0.0.0.0 --port 8000 &
API_PID=$!
echo "API server started with PID: $API_PID"

# Wait for either process to exit
wait -n

# If one exits, kill the other
kill $WORKER_PID $API_PID 2>/dev/null || true
exit 1
