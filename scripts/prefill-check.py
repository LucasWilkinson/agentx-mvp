#!/usr/bin/env python3
"""Correctness probe for a prefill server: send fixed prompts with max_tokens=1 and dump the
top-5 logprobs of the first generated token, so two deployments can be compared.
Env: IP PORT MODEL OUT(optional path, default stdout). Runs on the devbox."""
import json, os, random, sys, urllib.request
ip, port, model = os.environ["IP"], os.environ["PORT"], os.environ["MODEL"]
words = [f"w{i}" for i in range(5000)]
sizes = [300, 3000, 12000, 24000]  # words -> ~1k..82k tokens
results = []
for i, n in enumerate(sizes):
    rng = random.Random(1000 + i)
    prompt = " ".join(rng.choice(words) for _ in range(n))
    body = {"model": model, "prompt": prompt, "max_tokens": 1, "temperature": 0, "logprobs": 5}
    req = urllib.request.Request(f"http://{ip}:{port}/v1/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    r = json.loads(urllib.request.urlopen(req, timeout=900).read())
    ch = r["choices"][0]
    top = ch["logprobs"]["top_logprobs"][0]
    results.append({"words": n, "prompt_tokens": r["usage"]["prompt_tokens"], "text": ch["text"],
                    "top": dict(sorted(top.items(), key=lambda kv: -kv[1]))})
out = json.dumps(results, indent=1)
if os.environ.get("OUT"):
    open(os.environ["OUT"], "w").write(out)
print(out)
