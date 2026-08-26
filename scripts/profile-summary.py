#!/usr/bin/env python3
"""Summarise torch-profiler traces from one vLLM run: per-rank GPU busy time by kernel category.
Usage: profile-summary.py <trace_dir> [top_n]"""
import gzip, json, os, re, sys
from collections import defaultdict
d, top = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 25
CATS = [("notify|barrier|Barrier|sync", "comm-sync/wait"),
        ("sparse_attn|flash_mla|FlashMLA|flashmla|fmha|flash_fwd|attn_fwd|mla_", "attention"),
        ("mqa_logits|indexer|topKPerRow|topk|top_k|Topk|index_", "indexer/topk"),
        ("deep_ep|deepep|dispatch|combine|nvshmem|all_gather|allgather|AllGather|allreduce|all_reduce|AllReduce|ncclKernel|ncclDev", "comm"),
        ("deep_gemm|fp8_gemm|gemm|cutlass|nvjet|sm90_xmma|ampere_|matmul", "gemm"),
        ("moe|MoE|fused_experts|silu|act_and_mul|scatter|gather|permute|align_block", "moe-glue"),
        ("rms_norm|rmsnorm|layer_norm|rotary|rope|elementwise|vectorized|reduce_kernel|copy_|cat|fill|index_select|masked", "elementwise"),
        ("quant|scaled_fp8|per_token|cast|convert", "quant"),
        ("Memcpy|Memset", "memcpy")]
def cat(n):
    for pat, c in CATS:
        if re.search(pat, n): return c
    return "other"
files = sorted(f for f in os.listdir(d) if f.endswith(".json.gz") or f.endswith(".json"))
for f in files:
    p = os.path.join(d, f)
    ev = json.load(gzip.open(p) if p.endswith(".gz") else open(p))["traceEvents"]
    k = [e for e in ev if e.get("cat") == "kernel" and "dur" in e]
    if not k: continue
    t0, t1 = min(e["ts"] for e in k), max(e["ts"] + e["dur"] for e in k)
    by_name, by_cat, cnt = defaultdict(float), defaultdict(float), defaultdict(int)
    for e in k:
        by_name[e["name"]] += e["dur"]; by_cat[cat(e["name"])] += e["dur"]; cnt[e["name"]] += 1
    busy = sum(by_name.values())
    print(f"\n=== {f}: {len(k)} kernels, wall {(t1-t0)/1e3:.0f} ms, GPU busy {busy/1e3:.0f} ms ({100*busy/(t1-t0):.0f}%)")
    for c, v in sorted(by_cat.items(), key=lambda x: -x[1]): print(f"  {c:14} {v/1e3:8.0f} ms {100*v/busy:5.1f}%")
    print("  top kernels:")
    for n, v in sorted(by_name.items(), key=lambda x: -x[1])[:top]: print(f"    {v/1e3:8.0f} ms {cnt[n]:7d}x {v/cnt[n]:7.0f}us  {n[:100]}")
