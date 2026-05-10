# Quick Start Guide: Using Markdown Converter with RAG Template

## 🚀 Quick Start (Standalone Mode)

### Step 1: Start the Markdown Conversion Service

```bash
# Terminal 1: Start API
cd file-to-markdown-convertor
uvicorn app.api.main:app --host 0.0.0.0 --port 8000

# Terminal 2: Start Worker
cd file-to-markdown-convertor
./run_worker_standalone.sh
```

### Step 2: Verify Services Are Running

```bash
# Check API health
curl http://localhost:8000/health

# Should return: {"status":"healthy","service":"markdown-api"}
```

### Step 3: Test with 439.pdf

```bash
cd file-to-markdown-convertor
./test_real_pdfs.sh
```

Expected output:
```
✅ All PDF conversions successful!
   Simple PDF:  ✅ PASSED (21s)
   439.pdf:     ✅ PASSED (12s)
```

## 🐳 Docker Mode (Production)

```bash
cd file-to-markdown-convertor
docker compose up
```

This starts:
- 1 API server (port 8000)
- 4 worker processes (scalable)

Scale workers:
```bash
docker compose up --scale worker=8
```

## 📁 File Structure

```
file-to-markdown-convertor/
├── files_to_convert/     # Place PDFs here
│   └── 439.pdf          # Your PDF
├── converted_files/      # Markdown output appears here
│   └── 439.md           # Converted markdown
├── app/
│   ├── api/main.py      # FastAPI server
│   └── workers/worker.py # Conversion worker
└── tests/               # Test suite
```

## 🔄 Usage Flow

### 1. Submit Conversion

```bash
curl -X POST http://localhost:8000/convert \
  -H "Content-Type: application/json" \
  -d '{"file_path": "files_to_convert/439.pdf"}'
```

Response:
```json
{"conversion_id": "550e8400-e29b-41d4-a716-446655440000"}
```

### 2. Check Status

```bash
curl http://localhost:8000/convert/550e8400-e29b-41d4-a716-446655440000
```

Response:
```json
{"status": "completed"}  // or "pending", "processing", "failed"
```

### 3. Retrieve Converted File

```bash
curl http://localhost:8000/converted/439.md
```

Or just read it directly:
```bash
cat converted_files/439.md
```

## 🔧 Integration with RAG Template

The RAG template is already configured to use the markdown API:

```yaml
# In docker-compose.yml
rag-template:
  environment:
    - MARKDOWN_API_URL=http://markdown-api:8000
```

When you ingest PDFs in rag-template, they automatically:
1. Get sent to markdown-api for conversion
2. Convert using Docling
3. Return markdown with metadata
4. Get indexed into ChromaDB

## 🧪 Running Tests

### Run All Tests
```bash
cd file-to-markdown-convertor
./run_all_tests.sh all
```

### Run Specific Tests
```bash
# Unit tests only
./run_all_tests.sh unit

# Standalone integration test
./run_all_tests.sh standalone

# Docker integration test
./run_all_tests.sh docker

# Real PDF test (includes 439.pdf)
./test_real_pdfs.sh
```

## 📊 Expected Performance

| File | Size | Pages | Conversion Time |
|------|------|-------|----------------|
| Simple PDF | <1MB | 1-10 | 10-30s |
| **439.pdf** | 390KB | 8 | **12s** |
| Complex PDF | >1MB | 10-50 | 30-120s |

## ❌ Troubleshooting

### Problem: Worker stuck on "processing"

**Solution**: Make sure worker is running
```bash
# Check worker process
pgrep -f "workers/worker.py"

# If not running, start it
./run_worker_standalone.sh
```

### Problem: Connection refused

**Solution**: API not running
```bash
# Check API
curl http://localhost:8000/health

# If not running, start it
uvicorn app.api.main:app --reload
```

### Problem: Wrong host (Docker vs Standalone)

**Solution**: Worker auto-detects, but you can override
```bash
# Standalone
python -m app.workers.worker --host localhost

# Docker
python -m app.workers.worker --host api
```

### Problem: File not found

**Solution**: Check file path
```bash
# Files must be in files_to_convert/
ls files_to_convert/

# Use relative path from files_to_convert/
# Correct: "439.pdf" or "subdir/439.pdf"
# Wrong: "files_to_convert/439.pdf"
```

## 🎯 Key Features

✅ **Auto-detects** Docker vs Standalone  
✅ **Scalable** worker pool  
✅ **Async** status tracking  
✅ **Metadata** in frontmatter  
✅ **Health checks** included  
✅ **Comprehensive tests** 

## 📝 Output Format

Converted files include frontmatter metadata:

```markdown
---
source_file: files_to_convert/439.pdf
conversion_id: 550e8400-e29b-41d4-a716-446655440000
conversion_date: '2026-03-03T10:30:00.123456'
docling_name: '439'
docling_origin: DocumentOrigin.PDF
docling_num_pages: 8
---

# Document Title

Converted markdown content here...
```

## 🔗 Related Documentation

- [README.md](./README.md) - Full documentation
- [docs/archive/WORKER_FIX.md](./docs/archive/WORKER_FIX.md) - Technical details of the fix (archived)
- [docs/archive/TEST_RESULTS.md](./docs/archive/TEST_RESULTS.md) - Test results summary (archived)

## ✨ Success Criteria

Your setup is working correctly when:

1. ✅ `curl http://localhost:8000/health` returns healthy
2. ✅ `pgrep -f worker.py` shows running process
3. ✅ `./test_real_pdfs.sh` shows all tests passing
4. ✅ 439.pdf converts in ~12 seconds (not stuck!)

---

**Need help?** Check the logs:
- API logs: Check terminal where uvicorn is running
- Worker logs: Check terminal where worker is running
- Or run with explicit logging: `./test_real_pdfs.sh 2>&1 | tee test.log`
