#!/usr/bin/env python3
"""Write per-config context files the results chart shows under "Pod Specs", mirroring the reference
results layout: prefill.yaml / decode.yaml (model servers, from manifest.yaml), epp.yaml / epp-config.yaml /
inferencepool.yaml (router, from `helm get manifest` on stdin), and config_label.txt derived from spec.yaml.

Usage: run-context.py <config_dir> < router-manifest.yaml
"""
import os
import re
import sys

import yaml

config_dir = sys.argv[1]


def dump(path, docs):
    if docs:
        with open(path, "w") as f:
            yaml.safe_dump_all(docs, f, sort_keys=False)


# --- model servers by role
manifest = os.path.join(config_dir, "manifest.yaml")
if os.path.isfile(manifest):
    with open(manifest) as f:
        docs = [d for d in yaml.safe_load_all(f) if d]
    by_role = {"prefill": [], "decode": []}
    for d in docs:
        name = d["metadata"]["name"]
        role = d["metadata"].get("labels", {}).get("llm-d.ai/role") or next((r for r in by_role if f"-{r}" in name), None)
        if role in by_role:
            by_role[role].append(d)
    for role, rdocs in by_role.items():
        dump(os.path.join(config_dir, f"{role}.yaml"), rdocs)

# --- router objects (stdin)
router = [d for d in yaml.safe_load_all(sys.stdin) if d] if not sys.stdin.isatty() else []
dump(os.path.join(config_dir, "inferencepool.yaml"), [d for d in router if d["kind"] == "InferencePool"])
dump(os.path.join(config_dir, "epp-config.yaml"), [d for d in router if d["kind"] == "ConfigMap"])
dump(os.path.join(config_dir, "epp.yaml"), [d for d in router if d["kind"] not in ("InferencePool", "ConfigMap")])

# --- label: "[sweep] 1 x P (PCP8+EP) | 1 x D (DP8+EP)"
spec_path = os.path.join(config_dir, "spec.yaml")
if os.path.isfile(spec_path):
    with open(spec_path) as f:
        spec = yaml.safe_load(f)
    parts = []
    for role in spec.get("roles", []):
        par = role.get("parallelism", {})
        raw = " ".join(role.get("vllm_raw_args", []) or [])
        tp, dp, ep = int(par.get("tp", 1)), int(par.get("dp", 1)), bool(par.get("ep"))
        m = re.search(r"--prefill-context-parallel-size\s+(\d+)", raw)
        if m:
            layout = f"PCP{m.group(1)}"
        elif dp > 1:
            layout = f"DP{dp}" + (f"xTP{tp}" if tp > 1 else "")
        else:
            layout = f"TP{tp}"
        if ep:
            layout += "+EP"
        replicas = int(role.get("replicas", 1) or 1)
        parts.append(f"{replicas} x {role['name'][0].upper()} ({layout})")
    sweep = os.path.basename(os.path.dirname(os.path.abspath(config_dir)))
    with open(os.path.join(config_dir, "config_label.txt"), "w") as f:
        f.write(f"[{sweep}] " + " | ".join(parts) + "\n")
