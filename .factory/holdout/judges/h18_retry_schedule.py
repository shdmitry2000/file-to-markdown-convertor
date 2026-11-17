#!/usr/bin/env python3
"""H-18 judge. Usage: h18_retry_schedule.py record.json

record.json: request arrival times in seconds (floats) at the stub, per run:
{
  "always_429":       [t0, t1, ...],
  "always_500":       [t0, t1, ...],
  "retry_after_12":   [t0, t1, ...],
  "token_cap":        {"requests": [t0, ...], "pages": int, "status": "failed"},
  "llm_test_429":     [t0, ...]
}
Tolerance: +/-1.5 s per gap (plus 20% on the 429 gaps for jitter).
"""
import json, sys

TOL = 1.5

def gaps(ts):
    ts = sorted(ts)
    return [b - a for a, b in zip(ts, ts[1:])]

def main():
    try:
        r = json.load(open(sys.argv[1]))
    except Exception as e:
        print(f"INCONCLUSIVE: cannot read record: {e}"); return 2
    fails, missing = [], []

    t = r.get("always_429")
    if not t or len(t) < 3:
        missing.append("always_429")
    else:
        g = gaps(t)
        if abs(g[0] - 5) > TOL + 1:
            fails.append(f"429 first gap {g[0]:.1f}s, want ~5s")
        if len(g) > 1 and abs(g[1] - 10) > TOL + 2:
            fails.append(f"429 second gap {g[1]:.1f}s, want ~10s")
        if max(g) > 30 * 1.2 + TOL:
            fails.append(f"429 gap {max(g):.1f}s exceeds 30s cap")
        total = max(t) - min(t)
        if not (180 * 0.8 - TOL <= total <= 180 * 1.2 + 30):
            fails.append(f"429 retry window {total:.1f}s, want ~180s")

    t = r.get("always_500")
    if not t:
        missing.append("always_500")
    else:
        if len(t) != 3:
            fails.append(f"500: {len(t)} requests, want 3")
        else:
            g = gaps(t)
            if abs(g[0] - 1) > TOL or abs(g[1] - 2) > TOL:
                fails.append(f"500 gaps {g}, want ~[1, 2]")

    t = r.get("retry_after_12")
    if not t or len(t) < 2:
        missing.append("retry_after_12")
    elif abs(gaps(t)[0] - 12) > TOL + 1:
        fails.append(f"Retry-After first gap {gaps(t)[0]:.1f}s, want ~12s")

    tc = r.get("token_cap")
    if not tc or "requests" not in tc:
        missing.append("token_cap")
    else:
        n, pages = len(tc["requests"]), tc.get("pages", 0)
        if tc.get("status") != "failed":
            fails.append(f"token cap status {tc.get('status')!r}, want failed")
        # at once: no retries of the capped call, remaining pages not sent;
        # allow calls already in flight up to the configured concurrency (8)
        if n > 8 or (pages and n >= pages):
            fails.append(f"token cap: {n} requests for {pages} pages, want stop at once")
        if n > 1 and max(gaps(tc["requests"])) > TOL:
            fails.append("token cap: later requests sent after a delay (retried)")

    t = r.get("llm_test_429")
    if t is None:
        missing.append("llm_test_429")
    elif len(t) != 1:
        fails.append(f"/llm/test made {len(t)} requests, want 1")

    if fails:
        print("FAIL: " + "; ".join(fails)); return 1
    if missing:
        print("INCONCLUSIVE: missing " + ", ".join(missing)); return 2
    print("PASS"); return 0

if __name__ == "__main__":
    sys.exit(main())
