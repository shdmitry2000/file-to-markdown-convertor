"""TATR (microsoft/table-transformer-detection) vs docling-layout detection.

Question: is Microsoft's dedicated table-detection DETR a better/cheaper table
detector than docling's layout model for this Hebrew finance report?

Compares per-page table counts on a spread of the doc's Hebrew pages, reports
agreement + disagreements + speed. Runs on MPS (Mac GPU) if available.
"""
import io, time
import torch
import fitz
from PIL import Image
from transformers import AutoImageProcessor, TableTransformerForObjectDetection

from app.converters.dbank import DbankConverter, _heb_ratio

SRC = "/Users/dmitrysh/code/rag/rag_eval_bank/files_to_check/duah_hapoalim.pdf"
DEVICE = "mps" if torch.backends.mps.is_available() else "cpu"
THR = 0.7


def render(page, dpi=150):
    m = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=m)
    return Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")


def main():
    proc = AutoImageProcessor.from_pretrained("microsoft/table-transformer-detection")
    model = (TableTransformerForObjectDetection
             .from_pretrained("microsoft/table-transformer-detection")
             .to(DEVICE).eval())

    def tatr_count(img):
        inp = proc(images=img, return_tensors="pt").to(DEVICE)
        with torch.no_grad():
            out = model(**inp)
        sizes = torch.tensor([img.size[::-1]]).to(DEVICE)
        res = proc.post_process_object_detection(out, threshold=THR, target_sizes=sizes)[0]
        return int((res["scores"] > THR).sum())

    conv = DbankConverter()
    doc = fitz.open(SRC)
    # spread across the doc, Hebrew pages only, cap ~40
    idxs = [i for i in range(0, doc.page_count, 5)]
    rows = []
    t_dl = t_tatr = 0.0
    for i in idxs:
        page = doc[i]
        if _heb_ratio(page.get_text()) < 0.5:
            continue
        tmp = DbankConverter._write_page(doc, i)
        try:
            t0 = time.time(); _, dl = conv._docling_convert(str(tmp)); t_dl += time.time() - t0
        finally:
            tmp.unlink(missing_ok=True)
        img = render(page)
        t0 = time.time(); tt = tatr_count(img); t_tatr += time.time() - t0
        rows.append((i, dl, tt))
        if len(rows) >= 40:
            break

    print(f"\ndevice={DEVICE}  pages={len(rows)}")
    print(f"docling detect: {t_dl:.1f}s ({t_dl/len(rows)*1000:.0f} ms/pg)   "
          f"TATR detect: {t_tatr:.1f}s ({t_tatr/len(rows)*1000:.0f} ms/pg)")
    print(f"{'page':>5} {'docling':>8} {'tatr':>5}  note")
    both = dl_only = tatr_only = neither = 0
    for i, dl, tt in rows:
        note = ""
        if dl > 0 and tt > 0: both += 1
        elif dl > 0 and tt == 0: dl_only += 1; note = "<- docling sees table, TATR misses"
        elif dl == 0 and tt > 0: tatr_only += 1; note = "<- TATR sees table, docling misses"
        else: neither += 1
        print(f"{i:>5} {dl:>8} {tt:>5}  {note}")
    print(f"\nboth={both} docling_only={dl_only} tatr_only={tatr_only} neither={neither}")


if __name__ == "__main__":
    main()
