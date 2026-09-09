"""Prefiller comparison: mean / P99-free TTFT and request rate per concurrency, per P/D series.

Reads points.csv (from collect_points.py). Same INCLUDE/STYLE convention as plot_pareto.py:
only fixed-commit P/D series and the decoder-local baseline.
Render: cd /tmp && uv run --no-project --with matplotlib python <this file>
"""
import csv
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

here = Path(__file__).resolve().parent
STYLE = {  # label -> (color, marker)
    "PCP8+DCP8+EP8 -> TP8+EP8 (fixed 95eb419caf)": ("#d62728", "o"),
    "PCP8+EP8 (DCP1) -> TP8+EP8 (fixed 95eb419caf)": ("#1f77b4", "^"),
    "TP8+EP8 -> TP8+EP8 (fixed 95eb419caf)": ("#7047eb", "s"),
    "TP8+EP8 decoder-local (no P/D, 8 GPUs)": ("#7f7f7f", "x"),
    "PCP8+EP8 (DCP1) -> TP8+EP8 (no-54036 20a94597)": ("#1e3a8a", "v"),
    "PCP8+DCP8+EP8 -> TP8+EP8 (token-sharded simplify a6f09074)": ("#2ca02c", "P"),
}
SHORT = {
    "PCP8+DCP8+EP8 -> TP8+EP8 (fixed 95eb419caf)": "PCP8+DCP8+EP8 prefiller",
    "PCP8+EP8 (DCP1) -> TP8+EP8 (fixed 95eb419caf)": "PCP8+EP8 prefiller (DCP1)",
    "TP8+EP8 -> TP8+EP8 (fixed 95eb419caf)": "TP8+EP8 prefiller",
    "TP8+EP8 decoder-local (no P/D, 8 GPUs)": "no prefiller (decoder-local)",
    "PCP8+EP8 (DCP1) -> TP8+EP8 (no-54036 20a94597)": "PCP8+EP8 (DCP1), no PR 54036",
    "PCP8+DCP8+EP8 -> TP8+EP8 (token-sharded simplify a6f09074)": "PCP8+DCP8+EP8, token-sharded",
}

by_series = defaultdict(list)
with open(here / "points.csv") as fh:
    for r in csv.DictReader(fh):
        if r["series"] == "TP8+EP8 decoder-local (pre-fix)":  # the fix touched P/D only
            r["series"] = "TP8+EP8 decoder-local (no P/D, 8 GPUs)"
        if r["series"] in STYLE:
            by_series[r["series"]].append(r)

fig, axes = plt.subplots(1, 3, figsize=(15, 4.8))
for label, rows in by_series.items():
    rows.sort(key=lambda r: int(r["c"]))
    color, marker = STYLE[label]
    cs = [int(r["c"]) for r in rows]
    kw = dict(color=color, marker=marker, lw=1.8, ms=7, label=SHORT[label])
    axes[0].plot(cs, [float(r["ttft_ms"]) for r in rows], **kw)
    axes[1].plot(cs, [float(r["requests"]) / float(r["duration_s"]) for r in rows], **kw)
    axes[2].plot(cs, [float(r["output_tok_s"]) for r in rows], **kw)
for ax, ylab, title in (
    (axes[0], "mean TTFT (ms, log)", "Time to first token"),
    (axes[1], "completed requests / s", "Sustained request rate"),
    (axes[2], "output tokens / s", "Generation throughput"),
):
    ax.set_xscale("log", base=2); ax.set_xticks([1, 2, 4, 8]); ax.set_xticklabels(["c1", "c2", "c4", "c8"])
    ax.set_xlabel("evalscope concurrency (OpenHands conversations)")
    ax.set_ylabel(ylab); ax.set_title(title, fontsize=11); ax.grid(True, alpha=0.3, which="both")
axes[0].set_yscale("log")
axes[0].legend(fontsize=8, loc="upper left")
fig.suptitle("GLM-5.3 DSpark7 P/D on CoreWeave H200: prefiller layouts in front of the same TP8+EP8 decoder "
             "(LMSYS OpenHands, 220 output tokens; vllm 95eb419caf, plus no-54036 20a94597 and token-sharded simplify a6f09074)", fontsize=10)
fig.tight_layout()
for ext in ("svg", "png"):
    fig.savefig(here / f"h200-glm53-dspark7-prefiller-ttft.{ext}", dpi=150)
for label, rows in by_series.items():
    print(SHORT[label], " ".join(f"c{r['c']}:ttft={float(r['ttft_ms']):.0f}ms" for r in rows))
