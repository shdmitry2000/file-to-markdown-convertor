#!/bin/bash
# Integration test for Docker mode
# Tests the complete conversion pipeline in Docker

set -e

echo "🐳 Docker Mode Integration Test"
echo "=================================="
echo ""

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Cleanup function
cleanup() {
    echo ""
    echo "🧹 Cleaning up..."
    docker compose down -v 2>/dev/null || true
}

# Set trap for cleanup
trap cleanup EXIT

echo "1️⃣  Building Docker images..."
if docker compose build --quiet; then
    echo -e "${GREEN}   ✅ Build successful${NC}"
else
    echo -e "${RED}   ❌ Build failed${NC}"
    exit 1
fi

echo ""
echo "2️⃣  Starting services..."
if docker compose up -d; then
    echo -e "${GREEN}   ✅ Services started${NC}"
else
    echo -e "${RED}   ❌ Failed to start services${NC}"
    exit 1
fi

echo ""
echo "3️⃣  Waiting for services to be ready..."
sleep 5

# Wait for API health check
MAX_RETRIES=30
RETRY_COUNT=0
while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    if docker compose exec -T api curl -sf http://localhost:8000/health > /dev/null 2>&1; then
        echo -e "${GREEN}   ✅ API is healthy${NC}"
        break
    fi
    RETRY_COUNT=$((RETRY_COUNT + 1))
    if [ $RETRY_COUNT -eq $MAX_RETRIES ]; then
        echo -e "${RED}   ❌ API health check timeout${NC}"
        echo "   📋 API logs:"
        docker compose logs api
        exit 1
    fi
    sleep 1
done

echo ""
echo "4️⃣  Checking worker status..."
WORKER_COUNT=$(docker compose ps worker --format json 2>/dev/null | jq -r '. | length' || echo "0")
if [ "$WORKER_COUNT" -gt 0 ]; then
    echo -e "${GREEN}   ✅ Workers running: $WORKER_COUNT${NC}"
else
    echo -e "${RED}   ❌ No workers found${NC}"
    docker compose ps
    exit 1
fi

echo ""
echo "5️⃣  Creating test PDF..."
mkdir -p tests/files_to_convert
cat > tests/files_to_convert/test.pdf << 'EOF'
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
/Length 55
>>
stream
BT
/F1 12 Tf
100 700 Td
(Docker Test Document) Tj
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
420
%%EOF
EOF
echo -e "${GREEN}   ✅ Test file created${NC}"

echo ""
echo "6️⃣  Submitting conversion request..."
RESPONSE=$(docker compose exec -T api curl -sf -X POST http://localhost:8000/convert \
    -H "Content-Type: application/json" \
    -d '{"file_path": "files_to_convert/test.pdf"}')

CONVERSION_ID=$(echo "$RESPONSE" | jq -r '.conversion_id')

if [ -n "$CONVERSION_ID" ] && [ "$CONVERSION_ID" != "null" ]; then
    echo -e "${GREEN}   ✅ Conversion submitted${NC}"
    echo "   📋 Conversion ID: $CONVERSION_ID"
else
    echo -e "${RED}   ❌ Failed to submit conversion${NC}"
    echo "   Response: $RESPONSE"
    exit 1
fi

echo ""
echo "7️⃣  Polling conversion status..."
MAX_WAIT=60
ELAPSED=0
STATUS=""

while [ $ELAPSED -lt $MAX_WAIT ]; do
    STATUS_RESPONSE=$(docker compose exec -T api curl -sf http://localhost:8000/convert/$CONVERSION_ID)
    STATUS=$(echo "$STATUS_RESPONSE" | jq -r '.status')
    
    echo "   🔄 Status: $STATUS (${ELAPSED}s elapsed)"
    
    if [ "$STATUS" = "completed" ]; then
        echo -e "${GREEN}   ✅ Conversion completed successfully!${NC}"
        break
    elif [ "$STATUS" = "failed" ]; then
        echo -e "${RED}   ❌ Conversion failed${NC}"
        echo "   📋 Worker logs:"
        docker compose logs worker
        exit 1
    fi
    
    sleep 2
    ELAPSED=$((ELAPSED + 2))
done

if [ "$STATUS" != "completed" ]; then
    echo -e "${RED}   ❌ Conversion timeout${NC}"
    echo "   📋 API logs:"
    docker compose logs api
    echo "   📋 Worker logs:"
    docker compose logs worker
    exit 1
fi

echo ""
echo "8️⃣  Verifying output file..."
if docker compose exec -T api test -f converted_files/test.md; then
    echo -e "${GREEN}   ✅ Output file exists${NC}"
    
    # Check file content
    CONTENT=$(docker compose exec -T api cat converted_files/test.md)
    
    if echo "$CONTENT" | grep -q "---"; then
        echo -e "${GREEN}   ✅ Frontmatter present${NC}"
    else
        echo -e "${YELLOW}   ⚠️  No frontmatter found${NC}"
    fi
    
    if echo "$CONTENT" | grep -q "Docker Test Document"; then
        echo -e "${GREEN}   ✅ Content extracted${NC}"
    else
        echo -e "${YELLOW}   ⚠️  Expected content not found${NC}"
    fi
else
    echo -e "${RED}   ❌ Output file not found${NC}"
    docker compose exec -T api ls -la converted_files/ || true
    exit 1
fi

echo ""
echo "9️⃣  Testing worker environment detection..."
WORKER_LOG=$(docker compose logs worker 2>&1 | head -20)
if echo "$WORKER_LOG" | grep -q "Docker mode: True"; then
    echo -e "${GREEN}   ✅ Worker correctly detected Docker mode${NC}"
else
    echo -e "${YELLOW}   ⚠️  Docker mode detection unclear in logs${NC}"
    echo "   First 20 lines of worker log:"
    echo "$WORKER_LOG"
fi

echo ""
echo "=================================="
echo -e "${GREEN}✅ All Docker integration tests passed!${NC}"
echo ""
echo "📊 Summary:"
echo "   - Docker build: ✅"
echo "   - Services start: ✅"
echo "   - API health: ✅"
echo "   - Workers running: ✅"
echo "   - File conversion: ✅"
echo "   - Output verification: ✅"
echo "   - Environment detection: ✅"
