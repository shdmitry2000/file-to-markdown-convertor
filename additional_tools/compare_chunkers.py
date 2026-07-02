"""Head-to-head: default `hierarchical` (char windows) vs bank
`markdown_table_aware`, on real dbank markdown. Metric = table integrity:
a chunk holding table data rows WITHOUT their header separator is a broken
table (numbers stripped of column labels) — useless for retrieval.
"""
import importlib.util
from pathlib import Path

MD = Path("/tmp/dbank_slice.md").read_text(encoding="utf-8")

# --- load the bank chunker's pure logic directly (skip heavy builtins __init__)
_p = "/Users/dmitrysh/code/rag/rag_eval_bank/shared/rag_plugins/builtins/chunkers/bank/markdown_table_aware_chunker.py"
spec = importlib.util.spec_from_file_location("mtac", _p)
mtac = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mtac)


def hierarchical_chunks(text, child_size=500, overlap=50):
    """Replicate the default hierarchical chunker's leaf (retrieved) windows."""
    step = max(1, child_size - overlap)
    out, i = [], 0
    while i < len(text):
        out.append(text[i: i + child_size])
        if i + child_size >= len(text):
            break
        i += step
    return out


def table_aware_chunks(text, target=2800, overlap=200):
    chunks = []
    for kind, body, crumb in mtac._iter_blocks(text):
        pieces = (mtac._split_table(body, target) if kind == "table"
                  else mtac._split_prose(body, target, overlap))
        for pc in pieces:
            chunks.append(f"{crumb}\n\n{pc}" if crumb else pc)
    return chunks


def _has_data_row(t):
    return any(l.strip().startswith("|") and any(c.isdigit() for c in l)
               for l in t.splitlines())


def _has_separator(t):
    return any("---" in l and "|" in l for l in t.splitlines())


def _has_heading(t):
    return any(l.strip().startswith("#") for l in t.splitlines()) or " > " in t.split("\n")[0]


def report(name, chunks):
    n = len(chunks)
    orphans = sum(1 for c in chunks if _has_data_row(c) and not _has_separator(c))
    with_ctx = sum(1 for c in chunks if _has_heading(c))
    sizes = [len(c) for c in chunks]
    print(f"\n=== {name} ===")
    print(f"  chunks:                 {n}")
    print(f"  BROKEN table fragments: {orphans}  (data rows w/o column header)")
    print(f"  chunks w/ heading ctx:  {with_ctx}/{n} ({with_ctx*100//max(1,n)}%)")
    print(f"  size avg/max:           {sum(sizes)//max(1,n)}/{max(sizes)} chars")
    return orphans


def main():
    h = report("hierarchical (default, 500-char windows)", hierarchical_chunks(MD))
    m = report("markdown_table_aware (bank)", table_aware_chunks(MD))
    print(f"\nVERDICT: broken-table fragments  hierarchical={h}  table_aware={m}")
    # show a concrete broken fragment from hierarchical
    for c in hierarchical_chunks(MD):
        if _has_data_row(c) and not _has_separator(c):
            print("\n--- example hierarchical fragment (numbers, no column labels) ---")
            print(c[:280])
            break


if __name__ == "__main__":
    main()
