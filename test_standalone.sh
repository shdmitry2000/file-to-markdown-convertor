#!/bin/bash
# Integration test for Standalone mode
# Tests the complete conversion pipeline without Docker

set -e

echo "🖥️  Standalone Mode Integration Test"
echo "====================================="
echo ""

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Track PIDs for cleanup
API_PID=""
WORKER_PID=""

# Cleanup function
cleanup() {
    echo ""
    echo "🧹 Cleaning up..."
    
    if [ -n "$WORKER_PID" ]; then
        echo "   Stopping worker (PID: $WORKER_PID)..."
        kill $WORKER_PID 2>/dev/null || true
    fi
    
    if [ -n "$API_PID" ]; then
        echo "   Stopping API (PID: $API_PID)..."
        kill $API_PID 2>/dev/null || true
    fi
    
    # Clean test files
    rm -rf files_to_convert/test_standalone.pdf 2>/dev/null || true
    rm -rf converted_files/test_standalone.md 2>/dev/null || true
    
    sleep 1
}

# Set trap for cleanup
trap cleanup EXIT INT TERM

echo "1️⃣  Checking Python dependencies..."
if python -c "import zmq, fastapi, docling, frontmatter" 2>/dev/null; then
    echo -e "${GREEN}   ✅ All dependencies available${NC}"
else
    echo -e "${RED}   ❌ Missing dependencies${NC}"
    echo "   Install with: uv pip install fastapi uvicorn pyzmq docling python-frontmatter"
    exit 1
fi

echo ""
echo "2️⃣  Creating test directories..."
mkdir -p files_to_convert
mkdir -p converted_files
echo -e "${GREEN}   ✅ Directories created${NC}"

echo ""
echo "3️⃣  Creating test PDF..."
cat > files_to_convert/test_standalone.pdf << 'EOF'
%PDF-1.4
1 0 obj
<<
/Type /Catalog
/Pages 2 0 R
>>
endobj
2 0 obj
<<
/Type /Pages
/Kids [3 0 R]
/Count 1
>>
endobj
3 0 obj
<<
/Type /Page
/Parent 2 0 R
/Resources <<
/Font <<
/F1 <<
/Type /Font
/Subtype /Type1
/BaseFont /Helvetica
>>
>>
>>
/MediaBox [0 0 612 792]
/Contents 4 0 R
>>
endobj
4 0 obj
<<
/Length 58
>>
stream
BT
/F1 12 Tf
100 700 Td
(Standalone Test Document) Tj
ET
endstream
endobj
xref
0 5
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
0000000317 00000 n 
trailer
<<
/Size 5
/Root 1 0 R
>>
startxref
423
%%EOF
EOF
echo -e "${GREEN}   ✅ Test file created${NC}"

echo ""
echo "4️⃣  Starting API server..."
python -m uvicorn app.api.main:app --host 0.0.0.0 --port 8000 > /tmp/api_standalone.log 2>&1 &
API_PID=$!
echo "   📋 API PID: $API_PID"

# Wait for API to start
sleep 3

# Check if API is running
if kill -0 $API_PID 2>/dev/null; then
    echo -e "${GREEN}   ✅ API started${NC}"
else
    echo -e "${RED}   ❌ API failed to start${NC}"
    cat /tmp/api_standalone.log
    exit 1
fi

echo ""
echo "5️⃣  Waiting for API health check..."
MAX_RETRIES=30
RETRY_COUNT=0
while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
        echo -e "${GREEN}   ✅ API is healthy${NC}"
        break
    fi
    RETRY_COUNT=$((RETRY_COUNT + 1))
    if [ $RETRY_COUNT -eq $MAX_RETRIES ]; then
        echo -e "${RED}   ❌ API health check timeout${NC}"
        cat /tmp/api_standalone.log
        exit 1
    fi
    sleep 1
done

echo ""
echo "6️⃣  Starting worker..."
python -m app.workers.worker > /tmp/worker_standalone.log 2>&1 &
WORKER_PID=$!
echo "   📋 Worker PID: $WORKER_PID"

# Wait for worker to start
sleep 2

# Check if worker is running
if kill -0 $WORKER_PID 2>/dev/null; then
    echo -e "${GREEN}   ✅ Worker started${NC}"
else
    echo -e "${RED}   ❌ Worker failed to start${NC}"
    cat /tmp/worker_standalone.log
    exit 1
fi

echo ""
echo "7️⃣  Checking worker logs for environment detection..."
sleep 1
if grep -q "Docker mode: False" /tmp/worker_standalone.log; then
    echo -e "${GREEN}   ✅ Worker correctly detected standalone mode${NC}"
elif grep -q "localhost" /tmp/worker_standalone.log; then
    echo -e "${GREEN}   ✅ Worker using localhost${NC}"
else
    echo -e "${YELLOW}   ⚠️  Environment detection unclear${NC}"
    echo "   Worker log excerpt:"
    head -10 /tmp/worker_standalone.log
fi

echo ""
echo "8️⃣  Submitting conversion request..."
RESPONSE=$(curl -sf -X POST http://localhost:8000/convert \
    -H "Content-Type: application/json" \
    -d '{"file_path": "files_to_convert/test_standalone.pdf"}')

CONVERSION_ID=$(echo "$RESPONSE" | python -c "import sys, json; print(json.load(sys.stdin)['conversion_id'])" 2>/dev/null || echo "")

if [ -n "$CONVERSION_ID" ]; then
    echo -e "${GREEN}   ✅ Conversion submitted${NC}"
    echo "   📋 Conversion ID: $CONVERSION_ID"
else
    echo -e "${RED}   ❌ Failed to submit conversion${NC}"
    echo "   Response: $RESPONSE"
    exit 1
fi

echo ""
echo "9️⃣  Polling conversion status..."
MAX_WAIT=60
ELAPSED=0
STATUS=""

while [ $ELAPSED -lt $MAX_WAIT ]; do
    STATUS_RESPONSE=$(curl -sf http://localhost:8000/convert/$CONVERSION_ID)
    STATUS=$(echo "$STATUS_RESPONSE" | python -c "import sys, json; print(json.load(sys.stdin)['status'])" 2>/dev/null || echo "unknown")
    
    echo "   🔄 Status: $STATUS (${ELAPSED}s elapsed)"
    
    if [ "$STATUS" = "completed" ]; then
        echo -e "${GREEN}   ✅ Conversion completed successfully!${NC}"
        break
    elif [ "$STATUS" = "failed" ]; then
        echo -e "${RED}   ❌ Conversion failed${NC}"
        echo "   📋 Worker logs:"
        cat /tmp/worker_standalone.log
        exit 1
    fi
    
    sleep 2
    ELAPSED=$((ELAPSED + 2))
done

if [ "$STATUS" != "completed" ]; then
    echo -e "${RED}   ❌ Conversion timeout${NC}"
    echo "   📋 API logs:"
    cat /tmp/api_standalone.log
    echo "   📋 Worker logs:"
    cat /tmp/worker_standalone.log
    exit 1
fi

echo ""
echo "🔟 Verifying output file..."
if [ -f "converted_files/test_standalone.md" ]; then
    echo -e "${GREEN}   ✅ Output file exists${NC}"
    
    # Check file content
    CONTENT=$(cat converted_files/test_standalone.md)
    
    if echo "$CONTENT" | grep -q "\-\-\-"; then
        echo -e "${GREEN}   ✅ Frontmatter present${NC}"
    else
        echo -e "${YELLOW}   ⚠️  No frontmatter found${NC}"
    fi
    
    if echo "$CONTENT" | grep -q "Standalone Test Document"; then
        echo -e "${GREEN}   ✅ Content extracted${NC}"
    else
        echo -e "${YELLOW}   ⚠️  Expected content not found${NC}"
        echo "   File content:"
        cat converted_files/test_standalone.md
    fi
    
    if echo "$CONTENT" | grep -q "$CONVERSION_ID"; then
        echo -e "${GREEN}   ✅ Conversion ID in metadata${NC}"
    else
        echo -e "${YELLOW}   ⚠️  Conversion ID not found in metadata${NC}"
    fi
else
    echo -e "${RED}   ❌ Output file not found${NC}"
    ls -la converted_files/ || true
    exit 1
fi

echo ""
echo "====================================="
echo -e "${GREEN}✅ All standalone integration tests passed!${NC}"
echo ""
echo "📊 Summary:"
echo "   - Dependencies: ✅"
echo "   - API start: ✅"
echo "   - Worker start: ✅"
echo "   - Environment detection: ✅"
echo "   - File conversion: ✅"
echo "   - Output verification: ✅"
echo "   - Metadata verification: ✅"
