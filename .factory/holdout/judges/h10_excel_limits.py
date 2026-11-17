#!/usr/bin/env python3
"""H-10 judge. Usage: h10_excel_limits.py record.json

record.json:
{
  "default":        {"terms": int, "ellipsis": bool, "dependencies": bool},
  "max_refs_3":     {"terms": int, "ellipsis": bool},
  "deps_off":       {"dependencies": bool, "formulas": bool},
  "max_cells_1000": {"large_sheet_truncated": bool},
  "xls":            {"completed": bool, "formulas": bool}
}
Prints PASS, FAIL or INCONCLUSIVE (missing data) with reasons; exit 0/1/2.
"""
import json, sys

def main():
    try:
        r = json.load(open(sys.argv[1]))
    except Exception as e:
        print(f"INCONCLUSIVE: cannot read record: {e}"); return 2
    checks = [
        ("default", "terms", 20), ("default", "ellipsis", True),
        ("max_refs_3", "terms", 3), ("max_refs_3", "ellipsis", True),
        ("deps_off", "dependencies", False), ("deps_off", "formulas", True),
        ("max_cells_1000", "large_sheet_truncated", True),
        ("xls", "completed", True), ("xls", "formulas", False),
    ]
    fails, missing = [], []
    for run, key, want in checks:
        got = r.get(run, {}).get(key)
        if got is None:
            missing.append(f"{run}.{key}")
        elif got != want:
            fails.append(f"{run}.{key}={got!r}, want {want!r}")
    if fails:
        print("FAIL: " + "; ".join(fails)); return 1
    if missing:
        print("INCONCLUSIVE: missing " + ", ".join(missing)); return 2
    print("PASS"); return 0

if __name__ == "__main__":
    sys.exit(main())
