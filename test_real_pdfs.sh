#!/bin/bash
# Test script for real PDF files including 439.pdf
# Tests both simple PDFs and the problematic 439.pdf

set -e

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Track PIDs
API_PID=""
WORKER_PID=""

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
    
    sleep 1
}

trap cleanup EXIT INT TERM

echo ""
echo "╔═══════════════════════════════════════════════╗"
echo "║   Real PDF Conversion Test                    ║"
echo "║   Including 439.pdf                           ║"
echo "╚═══════════════════════════════════════════════╝"
echo ""

# Change to file-to-markdown-convertor directory
cd "$(dirname "$0")"

echo "1️⃣  Setting up test environment..."
mkdir -p files_to_convert
mkdir -p converted_files

# Copy test PDFs
echo "   Copying test PDFs..."

# Copy 439.pdf
if [ -f "../files_to_check/439.pdf" ]; then
    cp "../files_to_check/439.pdf" files_to_convert/
    echo -e "${GREEN}   ✅ Copied 439.pdf${NC}"
else
    echo -e "${RED}   ❌ 439.pdf not found at ../files_to_check/439.pdf${NC}"
    exit 1
fi

# Copy a simple PDF if available
if [ -f "../data/projects/test/raw/449.pdf" ]; then
    cp "../data/projects/test/raw/449.pdf" files_to_convert/simple.pdf
    echo -e "${GREEN}   ✅ Copied simple.pdf (449.pdf)${NC}"
fi

echo ""
echo "2️⃣  Starting API server..."
python -m uvicorn app.api.main:app --host 0.0.0.0 --port 8000 > /tmp/api_real_test.log 2>&1 &
API_PID=$!
echo "   📋 API PID: $API_PID"

sleep 3

if kill -0 $API_PID 2>/dev/null; then
    echo -e "${GREEN}   ✅ API started${NC}"
else
    echo -e "${RED}   ❌ API failed to start${NC}"
    cat /tmp/api_real_test.log
    exit 1
fi

echo ""
echo "3️⃣  Waiting for API health check..."
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
        cat /tmp/api_real_test.log
        exit 1
    fi
    sleep 1
done

echo ""
echo "4️⃣  Starting worker..."
python -m app.workers.worker > /tmp/worker_real_test.log 2>&1 &
WORKER_PID=$!
echo "   📋 Worker PID: $WORKER_PID"

sleep 2

if kill -0 $WORKER_PID 2>/dev/null; then
    echo -e "${GREEN}   ✅ Worker started${NC}"
else
    echo -e "${RED}   ❌ Worker failed to start${NC}"
    cat /tmp/worker_real_test.log
    exit 1
fi

# Check worker detected standalone mode
if grep -q "Docker mode: False" /tmp/worker_real_test.log; then
    echo -e "${GREEN}   ✅ Worker in standalone mode${NC}"
fi

# Function to test a PDF file
test_pdf() {
    local PDF_FILE=$1
    local TEST_NAME=$2
    local MAX_WAIT=${3:-180}  # Default 3 minutes for 439.pdf
    
    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}Testing: $TEST_NAME${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    
    echo ""
    echo "📤 Submitting conversion request for $PDF_FILE..."
    RESPONSE=$(curl -sf -X POST http://localhost:8000/convert \
        -H "Content-Type: application/json" \
        -d "{\"file_path\": \"files_to_convert/$PDF_FILE\"}")
    
    CONVERSION_ID=$(echo "$RESPONSE" | python -c "import sys, json; print(json.load(sys.stdin)['conversion_id'])" 2>/dev/null || echo "")
    
    if [ -n "$CONVERSION_ID" ]; then
        echo -e "${GREEN}   ✅ Conversion submitted${NC}"
        echo "   📋 Conversion ID: $CONVERSION_ID"
    else
        echo -e "${RED}   ❌ Failed to submit conversion${NC}"
        echo "   Response: $RESPONSE"
        return 1
    fi
    
    echo ""
    echo "⏳ Polling conversion status (max ${MAX_WAIT}s)..."
    ELAPSED=0
    STATUS=""
    LAST_STATUS=""
    
    while [ $ELAPSED -lt $MAX_WAIT ]; do
        STATUS_RESPONSE=$(curl -sf http://localhost:8000/convert/$CONVERSION_ID)
        STATUS=$(echo "$STATUS_RESPONSE" | python -c "import sys, json; print(json.load(sys.stdin)['status'])" 2>/dev/null || echo "unknown")
        
        # Only print if status changed
        if [ "$STATUS" != "$LAST_STATUS" ]; then
            echo "   🔄 Status: $STATUS (${ELAPSED}s elapsed)"
            LAST_STATUS=$STATUS
        fi
        
        if [ "$STATUS" = "completed" ]; then
            echo -e "${GREEN}   ✅ Conversion completed in ${ELAPSED}s!${NC}"
            
            # Verify output file
            OUTPUT_FILE="converted_files/${PDF_FILE%.pdf}.md"
            if [ -f "$OUTPUT_FILE" ]; then
                FILE_SIZE=$(wc -c < "$OUTPUT_FILE")
                echo -e "${GREEN}   ✅ Output file exists (${FILE_SIZE} bytes)${NC}"
                
                # Check for frontmatter
                if head -1 "$OUTPUT_FILE" | grep -q "\-\-\-"; then
                    echo -e "${GREEN}   ✅ Frontmatter present${NC}"
                fi
                
                # Show first few lines
                echo "   📄 First 5 lines of output:"
                head -5 "$OUTPUT_FILE" | sed 's/^/      /'
            else
                echo -e "${YELLOW}   ⚠️  Output file not found${NC}"
            fi
            
            return 0
        elif [ "$STATUS" = "failed" ]; then
            echo -e "${RED}   ❌ Conversion failed${NC}"
            echo "   📋 Worker logs (last 50 lines):"
            tail -50 /tmp/worker_real_test.log | sed 's/^/      /'
            return 1
        fi
        
        sleep 3
        ELAPSED=$((ELAPSED + 3))
        
        # Print progress dots every 15 seconds
        if [ $((ELAPSED % 15)) -eq 0 ]; then
            echo "   ⏱️  Still processing... (${ELAPSED}s)"
        fi
    done
    
    echo -e "${RED}   ❌ Conversion timeout after ${MAX_WAIT}s${NC}"
    echo "   Last status: $STATUS"
    return 1
}

# Test simple PDF first
if [ -f "files_to_convert/simple.pdf" ]; then
    test_pdf "simple.pdf" "Simple PDF (449.pdf)" 60
    SIMPLE_RESULT=$?
else
    SIMPLE_RESULT=0
    echo ""
    echo -e "${YELLOW}⏭️  Skipping simple PDF test (not available)${NC}"
fi

# Test the problematic 439.pdf
echo ""
test_pdf "439.pdf" "Problematic PDF (439.pdf)" 180
PDF_439_RESULT=$?

# Print summary
echo ""
echo "╔═══════════════════════════════════════════════╗"
echo "║   Test Summary                                 ║"
echo "╚═══════════════════════════════════════════════╝"
echo ""

if [ -f "files_to_convert/simple.pdf" ]; then
    if [ $SIMPLE_RESULT -eq 0 ]; then
        echo -e "   Simple PDF:  ${GREEN}✅ PASSED${NC}"
    else
        echo -e "   Simple PDF:  ${RED}❌ FAILED${NC}"
    fi
fi

if [ $PDF_439_RESULT -eq 0 ]; then
    echo -e "   439.pdf:     ${GREEN}✅ PASSED${NC}"
else
    echo -e "   439.pdf:     ${RED}❌ FAILED${NC}"
fi

echo ""

if [ $SIMPLE_RESULT -eq 0 ] && [ $PDF_439_RESULT -eq 0 ]; then
    echo -e "${GREEN}✅ All PDF conversions successful!${NC}"
    exit 0
else
    echo -e "${RED}❌ Some conversions failed${NC}"
    echo ""
    echo "📋 Check logs:"
    echo "   API:    /tmp/api_real_test.log"
    echo "   Worker: /tmp/worker_real_test.log"
    exit 1
fi
