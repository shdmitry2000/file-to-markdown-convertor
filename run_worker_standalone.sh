#!/bin/bash

# Run markdown conversion worker in standalone mode (outside Docker)
# This script automatically uses localhost for ZeroMQ connections

cd "$(dirname "$0")"

echo "Starting worker in standalone mode..."
echo "Connecting to: localhost:5555 (tasks) and localhost:5556 (results)"
echo ""

python -m app.workers.worker
