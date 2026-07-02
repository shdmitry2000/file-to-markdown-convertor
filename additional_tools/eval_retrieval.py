"""Golden-question retrieval eval: hierarchical vs markdown_table_aware, and a
TUNED table_aware that splits tables into small header+rows groups (granular +
self-contained). Questions include NATURAL/paraphrased phrasings, not just
verbatim labels. Metric: hit@1/3/5 + MRR (relevant = chunk contains gold text).
"""
import asyncio, importlib.util, re, sys
from pathlib import Path
import numpy as np

_p = "/Users/dmitrysh/code/rag/rag_eval_bank/shared/rag_plugins/builtins/chunkers/bank/markdown_table_aware_chunker.py"
_spec = importlib.util.spec_from_file_location("mtac", _p)
mtac = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(mtac)
from shared.llm_factory import LLMFactory


def table_aware(text, prose_target=2800, table_target=2800, overlap=200):
    out = []
    for kind, body, crumb in mtac._iter_blocks(text):
        pieces = (mtac._split_table(body, table_target) if kind == "table"
                  else mtac._split_prose(body, prose_target, overlap))
        for pc in pieces:
            out.append(f"{crumb}\n\n{pc}" if crumb else pc)
    return out


def distinctive_children(text, prose_target=2000, table_child=400):
    """Corrected retrieval units: prose = big chunks (with heading); table children
    = small RAW row-groups with NO repeated header/heading (distinctive -> avoids the
    near-duplicate-embedding collapse). Full table+header would be the PARENT."""
    out = []
    for kind, body, crumb in mtac._iter_blocks(text):
        if kind == "table":
            rows = body.split("\n")
            data = rows[2:] if len(rows) >= 3 else rows  # drop header+sep for children
            grp, ln = [], 0
            for r in data:
                if ln + len(r) > table_child and grp:
                    out.append("\n".join(grp)); grp, ln = [], 0
                grp.append(r); ln += len(r) + 1
            if grp:
                out.append("\n".join(grp))
        else:
            for pc in mtac._split_prose(body, prose_target, 50):
                out.append(f"{crumb}\n\n{pc}" if crumb else pc)
    return out


def hybrid(text, prose_target=2800, table_win=500, table_overlap=50):
    """Doc-type-aware: big chunks for prose (wins prose), hierarchical-style
    char-windows over each table region (wins tables — contiguous header+rows,
    distinctive per offset). Best-of-both candidate."""
    out = []
    step = max(1, table_win - table_overlap)
    for kind, body, crumb in mtac._iter_blocks(text):
        if kind == "table":
            seg = f"{crumb}\n{body}" if crumb else body  # heading + full table, then window
            i = 0
            while i < len(seg):
                out.append(seg[i:i + table_win])
                if i + table_win >= len(seg):
                    break
                i += step
        else:
            for pc in mtac._split_prose(body, prose_target, 50):
                out.append(f"{crumb}\n\n{pc}" if crumb else pc)
    return out


def hierarchical(text, child=500, overlap=50):
    step = max(1, child - overlap); out, i = [], 0
    while i < len(text):
        out.append(text[i:i + child])
        if i + child >= len(text):
            break
        i += step
    return out


def _norm(s):
    return re.sub(r"\s+", " ", s)


async def _embed(texts):
    vecs = await LLMFactory.embedding_client().embedding(texts)
    a = np.array(vecs, dtype=np.float32)
    a /= (np.linalg.norm(a, axis=1, keepdims=True) + 1e-9)
    return a


def evaluate(name, chunks, golden, qvecs):
    cvecs = asyncio.run(_embed(chunks))
    nc = [_norm(c) for c in chunks]
    hits = {1: 0, 3: 0, 5: 0}; rr = 0.0; misses = 0
    for gi, g in enumerate(golden):
        gold = _norm(g["gold"])
        relevant = {j for j, c in enumerate(nc) if gold in c}
        if not relevant:
            misses += 1; continue
        ranked = np.argsort(-(cvecs @ qvecs[gi]))
        rank = next((r for r, j in enumerate(ranked) if j in relevant), None)
        if rank is not None:
            for k in hits:
                hits[k] += rank < k
            rr += 1.0 / (rank + 1)
    n = len(golden)
    print(f"  [{name:<34}] chunks={len(chunks):>4}  "
          f"hit@1={hits[1]/n:.2f} hit@3={hits[3]/n:.2f} hit@5={hits[5]/n:.2f}  "
          f"MRR={rr/n:.3f}" + (f"  (gold-missing:{misses})" if misses else ""))
    return rr / n


def main(md_path, golden):
    text = Path(md_path).read_text(encoding="utf-8")
    qvecs = asyncio.run(_embed([g["q"] for g in golden]))
    print(f"\n===== {Path(md_path).name}  ({len(golden)} golden Qs) =====")
    evaluate("hierarchical(500) [old default]", hierarchical(text), golden, qvecs)
    evaluate("HYBRID [built: prose2800+tbl500]", hybrid(text), golden, qvecs)


GOLDEN_DUAH = [
    {"q": "כמה הרוויח הבנק? הרווח הנקי המיוחס לבעלי המניות", "gold": "רווח נקי המיוחס לבעלי מניות הבנק"},
    {"q": "מה היו הכנסות הריבית נטו של הבנק?", "gold": "הכנסות ריבית, נטו"},
    {"q": "מהי הלימות ההון של הבנק, יחס הון הליבה?", "gold": "יחס הון עצמי רובד 1 לרכיבי סיכון"},
    {"q": "הכנסות ממכשירים נגזרים למסחר", "gold": "הכנסות נטו בגין מכשירים נגזרים למסחר"},
    {"q": "כמה כסף הפריש הבנק להפסדי אשראי?", "gold": "הוצאות בגין הפסדי אשראי"},
    {"q": "מה היו ההוצאות התפעוליות של הבנק?", "gold": "הוצאות תפעוליות ואחרות"},
    {"q": "עד כמה הבנק יעיל בהוצאות? יחס היעילות", "gold": "יחס יעילות - הוצאות תפעוליות לסך ההכנסות"},
    {"q": "מה התשואה מריבית ביחס לנכסים הממוצעים?", "gold": "יחס הכנסות ריבית, נטו לנכסים ממוצעים"},
]

GOLDEN_368 = [
    {"q": "כמה זמן על הבנק להמשיך לתמוך בגרסה הקודמת של הסטנדרט לאחר עדכון?", "gold": "שישה חודשים מיום עליה לאוויר"},
    {"q": "מי אחראי לניהול סיכוני הבנקאות הפתוחה בבנק?", "gold": "הדירקטוריון אחראי"},
    {"q": "מהי רמת הסיכון של קבלת הרשאת גישה מלקוח?", "gold": "פעולה ברמת סיכון גבוהה"},
    {"q": "כמה הרשאות גישה מתמשכות מותר לנהל לכל אפליקציה?", "gold": "הרשאת גישה מתמשכת תקפה אחת בלבד"},
    {"q": "היכן מפרסם הבנק את השירותים שהוא מיישם עבור מפתחים?", "gold": "פורטל מפתחים"},
    {"q": "על מי חלה ההוראה?", "gold": "הוראה זו חלה על תאגיד בנקאי"},
    {"q": "איזו הוראת ניהול בנקאי תקין עוסקת בהגנת הסייבר?", "gold": "ניהול הגנת הסייבר"},
    {"q": "כיצד נותן הלקוח הרשאת גישה לנותן שירות מידע פיננסי?", "gold": "באופן מקוון"},
]


if __name__ == "__main__":
    doc = sys.argv[1] if len(sys.argv) > 1 else "duah"
    if doc == "368":
        main("/tmp/dbank_368.md", GOLDEN_368)
    else:
        main("/tmp/dbank_full.md", GOLDEN_DUAH)
