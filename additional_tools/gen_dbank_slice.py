"""Generate real dbank markdown for a table-heavy slice, to compare chunkers on."""
import os, tempfile
os.environ["DBANK_VLM_CONCURRENCY"] = "8"
from pathlib import Path
import fitz
from app.converters.dbank import DbankConverter

SRC = "/Users/dmitrysh/code/rag/rag_eval_bank/files_to_check/duah_hapoalim.pdf"
FROM, TO = 98, 105

doc = fitz.open(SRC)
sub = fitz.open()
sub.insert_pdf(doc, from_page=FROM, to_page=TO)
fd, name = tempfile.mkstemp(suffix="_slice.pdf")
os.close(fd)
sub.save(name)

md = DbankConverter().convert(Path(name))
Path("/tmp/dbank_slice.md").write_text(md, encoding="utf-8")
print(f"wrote /tmp/dbank_slice.md: {len(md)} chars, {md.count('|')} pipes")
os.unlink(name)
