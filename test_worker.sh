#!/bin/bash

# Quick test script for markdown conversion worker
# Tests both API and worker connectivity

set -e

echo "🧪 Testing Markdown Conversion Worker Setup"
echo "=========================================="
echo ""

# Check if API is running
echo "1️⃣  Checking API health..."
if curl -s http://localhost:8000/health > /dev/null 2>&1; then
    echo "   ✅ API is running on port 8000"
else
    echo "   ❌ API is NOT running on port 8000"
    echo "   💡 Start it with: uvicorn app.api.main:app --reload"
    exit 1
fi

echo ""
echo "2️⃣  Checking for test file..."
TEST_FILE="files_to_convert/439.pdf"
if [ -f "$TEST_FILE" ]; then
    echo "   ✅ Found test file: $TEST_FILE"
else
    echo "   ⚠️  Test file not found: $TEST_FILE"
    echo "   💡 Place your PDF in files_to_convert/ directory"
fi

echo ""
echo "3️⃣  Checking worker process..."
if pgrep -f "workers/worker.py" > /dev/null; then
    echo "   ✅ Worker is running"
    WORKER_PID=$(pgrep -f "workers/worker.py")
    echo "   📊 Worker PID: $WORKER_PID"
else
    echo "   ❌ Worker is NOT running"
    echo "   💡 Start it with: ./run_worker_standalone.sh"
    exit 1
fi

echo ""
echo "4️⃣  Submitting test conversion..."
if [ -f "$TEST_FILE" ]; then
    RESPONSE=$(curl -s -X POST http://localhost:8000/convert \
        -H "Content-Type: application/json" \
        -d "{\"file_path\": \"$TEST_FILE\"}")
    
    CONVERSION_ID=$(echo "$RESPONSE" | grep -o '"conversion_id":"[^"]*"' | cut -d'"' -f4)
    
    if [ -n "$CONVERSION_ID" ]; then
        echo "   ✅ Conversion submitted"
        echo "   📋 Conversion ID: $CONVERSION_ID"
        echo ""
        echo "5️⃣  Checking status..."
        sleep 2
        
        for i in {1..5}; do
            STATUS_RESPONSE=$(curl -s http://localhost:8000/convert/$CONVERSION_ID)
            STATUS=$(echo "$STATUS_RESPONSE" | grep -o '"status":"[^"]*"' | cut -d'"' -f4)
            echo "   🔄 Status: $STATUS"
            
            if [ "$STATUS" = "completed" ]; then
                echo "   ✅ Conversion completed successfully!"
                break
            elif [ "$STATUS" = "failed" ]; then
                echo "   ❌ Conversion failed"
                break
            fi
            
            sleep 3
        done
    else
        echo "   ❌ Failed to submit conversion"
        echo "   Response: $RESPONSE"
    fi
else
    echo "   ⏭️  Skipping test (no test file)"
fi

echo ""
echo "=========================================="
echo "✅ Test complete!"
echo ""
echo "💡 Tips:"
echo "   - Watch API logs: tail -f api.log"
echo "   - Watch worker logs in the terminal where you started it"
echo "   - Check converted files in: converted_files/"
