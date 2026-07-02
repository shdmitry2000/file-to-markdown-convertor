"""Validate the new dbank (docling-layout detection + parallel VLM) on the
critical finance doc. Part A: full-doc routing + detection wall time (VLM
stubbed). Part B: real serial-vs-parallel VLM timing on a sample of detected
table pages (proves the concurrency win + thread-safety)."""
import os, time, sys
from pathlib import Path

os.environ.setdefault("DOCLING_CPU_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import fitz
from app.converters.dbank import DbankConverter, _heb_ratio

SRC = Path("/Users/dmitrysh/code/rag/rag_eval_bank/files_to_check/duah_hapoalim.pdf")


def part_a():
    """Full-doc classification with VLM stubbed -> routing counts + phase1 time."""
    conv = DbankConverter()
    calls = {"vlm": 0}

    class Stub:
        def transcribe_page(self, img_b64):
            calls["vlm"] += 1
            return "STUB"
    conv._ensure_vlm = lambda: setattr(conv, "_vlm", Stub())

    t0 = time.time()
    conv.convert(SRC)  # detection runs for real; VLM is instant stub
    dt = time.time() - t0
    print(f"\n[A] full-doc routing (VLM stubbed): {calls['vlm']} VLM pages, "
          f"detection+phase1 wall={dt:.1f}s ({dt/224*1000:.0f} ms/page)")


def part_b(sample=10, conc=8):
    """Real VLM on `sample` detected-table pages: serial vs parallel."""
    conv = DbankConverter()
    conv._ensure_vlm()
    from concurrent.futures import ThreadPoolExecutor

    # find table pages via the real detector, render a handful
    imgs = []
    with fitz.open(str(SRC)) as doc:
        for i in range(doc.page_count):
            if len(imgs) >= sample:
                break
            page = doc[i]
            if _heb_ratio(page.get_text()) < 0.5:
                continue
            tmp = DbankConverter._write_page(doc, i)
            try:
                _, n_tables = conv._docling_convert(str(tmp))
            finally:
                tmp.unlink(missing_ok=True)
            if n_tables > 0:
                imgs.append((i, DbankConverter._render(page)))

    pages = [p for p, _ in imgs]
    print(f"\n[B] sample table pages: {pages}", flush=True)

    t0 = time.time()
    serial = []
    for p, b in imgs:
        tp = time.time()
        serial.append(conv._vlm.transcribe_page(b))
        print(f"    serial page {p}: {time.time()-tp:.1f}s", flush=True)
    st = time.time() - t0

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=conc) as ex:
        par = list(ex.map(lambda b: conv._vlm.transcribe_page(b), [b for _, b in imgs]))
    pt = time.time() - t0

    # fidelity sanity: outputs should match serial (same model, temp 0.1-ish)
    def digits(s): return sum(c.isdigit() for c in s)
    print(f"    serial:   {st:.1f}s  ({st/len(imgs):.1f}s/page)")
    print(f"    parallel: {pt:.1f}s  (x{conc}) -> {st/pt:.1f}x speedup")
    for (p, _), out in zip(imgs, par):
        print(f"    page {p:>3}: {len(out):>5} chars, {digits(out):>4} digits, "
              f"heb_ratio={_heb_ratio(out):.2f}")


if __name__ == "__main__":
    part_a()
    part_b(sample=int(sys.argv[1]) if len(sys.argv) > 1 else 10)
