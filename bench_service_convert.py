"""Measure our markdown-api service's pure conversion time per file (no ingest).

Submits each PDF to markdown-api /convert, polls fast (0.2s) for accurate
server-side timing, sequentially (one at a time). Compare against
bench_docling_standalone.py (~0.6s/file warm) to expose service overhead.
"""
import sys
import time
import requests

API = "http://localhost:7001"


def convert_one(pdf_path: str) -> tuple:
    t0 = time.perf_counter()
    r = requests.post(f"{API}/convert", json={"file_path": pdf_path, "converter_type": "docling"}, timeout=30)
    r.raise_for_status()
    cid = r.json()["conversion_id"]
    status = None
    while time.perf_counter() - t0 < 120:
        time.sleep(0.2)
        s = requests.get(f"{API}/convert/{cid}", timeout=10).json()
        status = s.get("status")
        if status in ("completed", "success", "failed"):
            break
    return status, time.perf_counter() - t0


def main(pdfs):
    for i, pdf in enumerate(pdfs, 1):
        status, dt = convert_one(pdf)
        print(f"[service convert {i}] {pdf.split('/')[-1]}: {dt:.2f}s  status={status}")


if __name__ == "__main__":
    main(sys.argv[1:])
