#!/usr/bin/env python3
"""Throughput-interactivity Pareto for the H200 GLM-5.3 DSpark7 P/D matrix.

x: interactivity = 1000 / mean TPOT (output tokens/s/user); y: EvalScope logical
total-token throughput (prompt incl. cache hits + output) per GPU. P/D arms use
16 H200s (8 prefill + 8 decode); the decoder-local point uses 8. Solid, outlined
points are Pareto-efficient within their series; faded ones are dominated.
"""
import csv, sys
from collections import defaultdict
from pathlib import Path
import matplotlib.pyplot as plt

plt.rcParams["svg.fonttype"] = "none"
here = Path(__file__).parent
rows = list(csv.DictReader(open(here / "points.csv")))
STYLE = {
    "PCP8+DCP8+EP8 -> TP8+EP8 (fixed 95eb419caf)": ("#0f9fa8", "o", 1.0),
    "PCP8+EP8 (DCP1) -> TP8+EP8 (fixed 95eb419caf)": ("#2563eb", "^", 1.0),
    "TP8+EP8 -> TP8+EP8 (fixed 95eb419caf)": ("#7047eb", "s", 1.0),
    "TP8+EP8 decoder-local (no P/D, 8 GPUs)": ("#f59e0b", "D", 1.0),
    "PCP8+EP8 (DCP1) -> TP8+EP8 (no-54036 20a94597)": ("#1e3a8a", "v", 1.0),
    "PCP8+DCP8+EP8 -> TP8+EP8 (token-sharded simplify a6f09074)": ("#dc2626", "P", 1.0),
    "PCP8+EP8 (DCP1) -> TP8+EP8 (no direct KV 1ca5131a07)": ("#0891b2", "<", 1.0),
    "PCP8+EP8 (DCP1) -> TP8+EP8 (no direct KV, prefill cudagraph NONE)": ("#ea580c", ">", 1.0),
}
OFFSETS = {
    "PCP8+DCP8+EP8 -> TP8+EP8 (fixed 95eb419caf)": (8, 5),
    "PCP8+EP8 (DCP1) -> TP8+EP8 (fixed 95eb419caf)": (8, -24),
    "TP8+EP8 -> TP8+EP8 (fixed 95eb419caf)": (-14, -26),
    "TP8+EP8 decoder-local (no P/D, 8 GPUs)": (8, 5),
    "PCP8+EP8 (DCP1) -> TP8+EP8 (no-54036 20a94597)": (-40, 6),
    "PCP8+DCP8+EP8 -> TP8+EP8 (token-sharded simplify a6f09074)": (8, -26),
    "PCP8+EP8 (DCP1) -> TP8+EP8 (no direct KV 1ca5131a07)": (-46, -22),
    "PCP8+EP8 (DCP1) -> TP8+EP8 (no direct KV, prefill cudagraph NONE)": (10, 8),
}
INCLUDE = set(STYLE)  # pre-fix P/D series are excluded from the chart
by = defaultdict(list)
for r in rows:
    if r["series"] == "TP8+EP8 decoder-local (pre-fix)":
        r["series"] = "TP8+EP8 decoder-local (no P/D, 8 GPUs)"
    if r["series"] not in INCLUDE:
        continue
    by[r["series"]].append((int(r["c"]), float(r["interactivity"]), float(r["total_tok_s_per_gpu"]), float(r["ttft_ms"]), float(r["decoded_tok_iter"])))
def pareto(pts):
    return {(x, y) for _, x, y, _, _ in pts if not any(ox >= x and oy >= y and (ox > x or oy > y) for _, ox, oy, _, _ in pts)}
plt.style.use("seaborn-v0_8-whitegrid")
fig, ax = plt.subplots(figsize=(12, 7.5), constrained_layout=True)
for label, pts in by.items():
    color, marker, alpha = STYLE[label]
    pts.sort()
    eff = pareto(pts)
    xs = [p[1] for p in pts]; ys = [p[2] for p in pts]
    ax.plot(xs, ys, color=color, marker=marker, linewidth=1.6, markersize=8, alpha=0.25 * alpha, label=label,
            linestyle="--" if alpha < 1 else "-")
    for c, x, y, ttft, tokit in pts:
        on = (x, y) in eff
        ax.scatter([x], [y], s=110 if on else 60, color=color, marker=marker, alpha=alpha if on else 0.35 * alpha,
                   edgecolors="black" if on and alpha == 1 else "none", linewidths=1.2, zorder=3)
        if alpha == 1:
            # Six series crowd the 160-200 interactivity band; fan the labels out
            # on a per-series angle so coincident points stay legible.
            ax.annotate(f"c{c}\n{ttft/1000:.1f}s TTFT", (x, y), textcoords="offset points",
                        xytext=OFFSETS[label], fontsize=7.5, color=color)
ax.set_xlabel("Interactivity: 1000 / mean TPOT (output tokens/s/user) →", fontsize=11)
ax.set_ylabel("Logical total-token throughput per GPU (tokens/s) →", fontsize=11)
ax.set_title("GLM-5.3 DSpark7 P/D on CoreWeave H200 · LMSYS OpenHands (evalscope, 220 output tokens)\n"
             "fork 95eb419caf plus the no-direct-KV lineage; P/D arms use 16 H200s (8 prefill + 8 decode); decoder-local = the TP8 decoder prefilling itself, 8 GPUs, c1 only\n"
             "DCP8 arms differ by transport: 95eb419caf reads peer KV via symmetric memory; simplify a6f09074 has no direct KV and uses the token-sharded exchange", fontsize=10)
ax.legend(loc="lower left", fontsize=8.5, framealpha=0.93)
ax.set_xlim(left=60)
ax.set_ylim(bottom=0)
for ext in ("svg", "png"):
    fig.savefig(here / f"h200-glm53-dspark7-pd-pareto.{ext}", dpi=150)
print("wrote", here / "h200-glm53-dspark7-pd-pareto.png")
