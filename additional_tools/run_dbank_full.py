"""Full end-to-end dbank timing on duah_hapoalim.pdf via the factory (3.5-flash)."""
import os, time, logging
os.environ.setdefault("DBANK_VLM_CONCURRENCY", "8")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
from pathlib import Path
from app.converters.dbank import DbankConverter

SRC = "/Users/dmitrysh/code/rag/rag_eval_bank/files_to_check/duah_hapoalim.pdf"

t0 = time.time()
md = DbankConverter().convert(Path(SRC))
dt = time.time() - t0
Path("/tmp/dbank_full.md").write_text(md, encoding="utf-8")
print(f"TOTAL {dt:.1f}s  ({dt/60:.1f} min)  {len(md)} chars  {md.count('|')} table-pipes  "
      f"conc={os.environ['DBANK_VLM_CONCURRENCY']}")
