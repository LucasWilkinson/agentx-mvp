#!/usr/bin/env python3
"""Warm a prefill server with random long prompts, capture one torch-profiler trace of a single prefill.
Env: IP PORT WORDS MODEL NAME. Runs on the devbox (needs pod-network access)."""
import json, os, random, time, urllib.request
ip, port, words, model, name = os.environ["IP"], os.environ["PORT"], int(os.environ["WORDS"]), os.environ["MODEL"], os.environ["NAME"]
# CONC>1 sends one prompt per port PORT..PORT+CONC-1 concurrently (multi-port external-LB DP ranks).
conc = int(os.environ.get("CONC", "1"))
import threading
base = f"http://{ip}:{port}"
vocab = [f"w{i}" for i in range(5000)]
def post(path, body=None):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else b"", headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=600) as r: return json.loads(r.read() or b"{}")
def prefill_one(seed, p):
    rng = random.Random(seed)
    prompt = " ".join(rng.choice(vocab) for _ in range(words))
    t = time.time()
    req = urllib.request.Request(f"http://{ip}:{p}/v1/completions", data=json.dumps({"model": model, "prompt": prompt, "max_tokens": 1, "temperature": 0}).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=600) as r: n = json.loads(r.read())["usage"]["prompt_tokens"]
    return time.time() - t, n
def prefill(seed):
    if conc == 1: return prefill_one(seed, port)
    out = [None] * conc
    def run(i): out[i] = prefill_one(seed * 100 + i, int(port) + i)
    ts = [threading.Thread(target=run, args=(i,)) for i in range(conc)]
    t = time.time(); [x.start() for x in ts]; [x.join() for x in ts]
    return time.time() - t, sum(n for _, n in out)  # aggregate tokens over all ranks, wall of the slowest
for i in range(3):
    dt, n = prefill(100 + i); print(f"[{name}] warm{i}: {n} tokens in {dt:.2f}s = {n/dt:.0f} tok/s", flush=True)
post("/start_profile"); time.sleep(1)
dt, n = prefill(999); print(f"[{name}] profiled: {n} tokens in {dt:.2f}s = {n/dt:.0f} tok/s", flush=True)
post("/stop_profile"); print("profile stopped", flush=True)
