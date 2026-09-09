#!/usr/bin/env python3
"""Collect H200 GLM-5.3 DSpark P/D sweep points into a CSV for the Pareto chart."""
import csv, glob, json, os, pathlib, re
ROOT = "results/.artifacts/reproductions/glm53-frankenstein-clean-dspark-matrix-h200"
SERIES = [  # (label, sweep dir, arm dir, points subdir, gpus)
    ("PCP8+DCP8+EP8 -> TP8+EP8 (fixed 95eb419caf)", "20260908T133000Z-95eb419caf", "clean-pcp8-dcp8-ep8-dspark-tp8", "points", 16),
    ("PCP8+EP8 (DCP1) -> TP8+EP8 (fixed 95eb419caf)", "20260908T133000Z-95eb419caf", "clean-pcp8-ep8-dspark-tp8", "points", 16),
    ("TP8+EP8 -> TP8+EP8 (fixed 95eb419caf)", "20260908T133000Z-95eb419caf", "clean-tp8-ep8-dspark-tp8", "points", 16),
    ("PCP8+DCP8+EP8 -> TP8+EP8 (pre-fix ce18ca07d9)", "20260907T233000Z-b9914a14b8", "clean-pcp8-dcp8-ep8-dspark-tp8", "points", 16),
    ("PCP8+EP8 (DCP1) -> TP8+EP8 (pre-fix e7ede2722b)", "20260907T210000Z-e7ede2722b", "clean-pcp8-ep8-dspark-tp8", "points", 16),
    ("TP8+EP8 decoder-local (pre-fix)", "20260907T233000Z-b9914a14b8", "clean-pcp8-ep8-dspark-tp8", "points-decoder-local", 8),
    ("PCP8+EP8 (DCP1) -> TP8+EP8 (no-54036 20a94597)", "20260909T140000Z-no54036-20a94597", "no54036-pcp8-ep8-dspark-tp8", "points", 16),
    ("PCP8+DCP8+EP8 -> TP8+EP8 (token-sharded simplify a6f09074)", "20260909T150000Z-simplify-a6f09074", "simplify-pcp8-dcp8-ep8-dspark-tp8", "points", 16),
    # Re-measured 2026-09-09: the original 20260909T050000Z sweep was deleted before
    # its TPOT / total-token columns were transcribed, so the arm could not be plotted.
    ("PCP8+EP8 (DCP1) -> TP8+EP8 (no direct KV 1ca5131a07)", "20260909T183000Z-nodk-1ca5131a", "nodk-pcp8-ep8-dspark-tp8", "points", 16),
]
out = str(pathlib.Path(__file__).resolve().parent / "points.csv")
# Older sweeps' raw points were purged; their aggregated rows survive only in
# points.csv. Seed from it and let anything still on disk overwrite in place, so
# re-running never silently drops a series whose source directory is gone.
existing = {}
if os.path.exists(out):
    with open(out, newline="") as fh:
        for r in csv.DictReader(fh):
            existing[(r["series"], int(r["c"]))] = r

rows = []
for label, sweep, arm, sub, gpus in SERIES:
    best = {}
    for f in glob.glob(os.path.join(ROOT, sweep, arm, sub, "*", "*", "benchmark_summary.json")):
        pdir = os.path.basename(os.path.dirname(os.path.dirname(f)))
        m = re.search(r"-c(\d+)-(?:decoder-local-)?(\d{8}T\d{6}Z)$", pdir)
        if not m: continue
        c, ts = int(m.group(1)), m.group(2)
        if c in best and best[c][0] > ts: continue  # keep the latest point per concurrency
        best[c] = (ts, f)
    for c, (ts, f) in sorted(best.items()):
        d = json.load(open(f))
        if d["Failed Requests"]: continue
        rows.append(dict(series=label, gpus=gpus, c=c, point=ts, requests=d["Total Requests"], duration_s=round(d["Test Duration (s)"], 1),
                         ttft_ms=d["TTFT (ms)"], tpot_ms=d["TPOT (ms)"], interactivity=round(1000 / d["TPOT (ms)"], 2),
                         output_tok_s=round(d["Output Throughput (tok/s)"], 1), total_tok_s=round(d["Total Throughput (tok/s)"], 1),
                         total_tok_s_per_gpu=round(d["Total Throughput (tok/s)"] / gpus, 1), output_tok_s_per_gpu=round(d["Output Throughput (tok/s)"] / gpus, 2),
                         decoded_tok_iter=d.get("Decoded Tok/Iter"), spec_accept=d.get("Spec. Accept Rate")))
found = {(r["series"], r["c"]) for r in rows}
merged = list(rows) + [r for k, r in existing.items() if k not in found]
order = {label: i for i, (label, *_) in enumerate(SERIES)}
merged.sort(key=lambda r: (order.get(r["series"], len(order)), int(r["c"])))
fields = list(rows[0].keys()) if rows else list(next(iter(existing.values())).keys())
with open(out, "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=fields); w.writeheader(); w.writerows(merged)
rows = merged
for r in rows:
    f2 = lambda v: float(v)
    print(f"{r['series'][:52]:52s} c{r['c']} tpot={f2(r['tpot_ms']):5.1f} inter={f2(r['interactivity']):6.1f} total/gpu={f2(r['total_tok_s_per_gpu']):8.1f} out/gpu={f2(r['output_tok_s_per_gpu']):6.2f} ttft={f2(r['ttft_ms']):7.1f} tok/it={r['decoded_tok_iter']}")
