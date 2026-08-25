#!/usr/bin/env python3
"""Parse vLLM startup log lines (stdin) into YAML facts: weights, KV cache size, max concurrency, auto-fit, graphs."""
import re
import sys

PATTERNS = [
    (r"Model loading took ([\d.]+) GiB", lambda m: [("weights_gib", m.group(1))]),
    (r"Available KV cache memory: ([\d.]+) GiB", lambda m: [("kv_cache_gib", m.group(1))]),
    (r"GPU KV cache size: ([\d,]+) tokens", lambda m: [("kv_cache_tokens", m.group(1).replace(",", ""))]),
    (r"Maximum concurrency for ([\d,]+) tokens per request: ([\d.]+)x",
     lambda m: [("max_model_len", m.group(1).replace(",", "")), ("max_concurrency_at_max_model_len", m.group(2))]),
    (r"Auto-fit max_model_len: (.*)", lambda m: [("auto_fit", '"' + m.group(1).strip() + '"')]),
    (r"Graph capturing finished in \d+ secs, took ([\d.]+) GiB", lambda m: [("cudagraph_gib", m.group(1))]),
]
seen = set()
for line in sys.stdin:
    for pat, emit in PATTERNS:
        if m := re.search(pat, line):
            for key, value in emit(m):
                if key not in seen:
                    seen.add(key)
                    print(f"  {key}: {value}")
