#!/usr/bin/env python3
"""Turn a rendered manifesto manifest (stdin) into readable per-role YAML of the effective vLLM server args.

For each model-server workload with a `vllm` container: find the `vllm serve ...` command in its launch
script, shell-split it, resolve simple `NAME=value` shell variables assigned earlier in the script, and fold
flags into a mapping (a flag given twice keeps the last value, like argparse). Env vars set on the container
are listed too. Output: {role: {model, args: {...}, env: {...}}}.
"""
import json
import re
import shlex
import sys

import yaml

WORKLOADS = {"Deployment", "LeaderWorkerSet", "StatefulSet"}


def _pod_specs(doc):
    kind = doc.get("kind")
    if kind not in WORKLOADS:
        return
    spec = doc.get("spec", {})
    if kind == "LeaderWorkerSet":
        lws = spec.get("leaderWorkerTemplate", {})
        for key in ("leaderTemplate", "workerTemplate"):
            if lws.get(key):
                yield key, lws[key]["spec"]
    else:
        yield "", spec["template"]["spec"]


def _serve_command(script):
    for line in script.replace("\\\n", " ").splitlines():
        s = line.strip()
        if s.startswith("exec "):
            s = s[5:].strip()
        # allow an env-prefixed command: `FOO=bar BAZ=qux vllm serve ...`
        m = re.match(r"(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(vllm\s+serve\b.*)", s)
        if m:
            return m.group(1).rstrip(" &")
    return None


def _shell_vars(script):
    env = {}
    for m in re.finditer(r"^\s*([A-Z_][A-Z0-9_]*)=([^\n;]+)$", script, re.M):
        name, raw = m.group(1), m.group(2).strip()
        if raw.startswith("$(") or raw.startswith("("):
            continue
        env[name] = raw.strip("\"'")
    return env


def _resolve(tok, shell_vars):
    return re.sub(r"\$\{?([A-Z_][A-Z0-9_]*)\}?", lambda m: shell_vars.get(m.group(1), m.group(0)), tok)


def _coerce(value):
    if value is None:
        return True
    if value.startswith("{") or value.startswith("["):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            pass
    return value


def parse_serve(cmd, shell_vars):
    toks = [_resolve(t, shell_vars) for t in shlex.split(cmd)]
    assert toks[:2] == ["vllm", "serve"], cmd
    model = toks[2]
    args = {}
    i = 3
    while i < len(toks):
        tok = toks[i]
        if not tok.startswith("-") or re.fullmatch(r"-\d+(\.\d+)?", tok):
            args.setdefault("_positional", []).append(tok)
            i += 1
            continue
        if "=" in tok:
            key, value = tok.split("=", 1)
            i += 1
        elif i + 1 < len(toks) and (not toks[i + 1].startswith("-") or re.fullmatch(r"-\d+(\.\d+)?", toks[i + 1])):
            key, value = tok, toks[i + 1]
            i += 2
        else:
            key, value = tok, None
            i += 1
        key = key.lstrip("-").replace("_", "-")
        if key.startswith("cc."):
            key = "compilation-config." + key[3:]
        args[key] = _coerce(value)
    return model, args


def main():
    out = {}
    for doc in yaml.safe_load_all(sys.stdin):
        if not doc:
            continue
        role = doc.get("metadata", {}).get("labels", {}).get("llm-d.ai/role") or doc["metadata"]["name"]
        for suffix, pod in _pod_specs(doc):
            for c in pod.get("containers", []):
                if c.get("name") != "vllm":
                    continue
                script = "\n".join(c.get("args", []))
                cmd = _serve_command(script)
                if not cmd:
                    continue
                model, args = parse_serve(cmd, _shell_vars(script))
                env = {e["name"]: e.get("value", "<valueFrom>") for e in c.get("env", [])}
                out[f"{role}/{suffix}" if suffix else role] = {"model": model, "args": args, "env": env}
    yaml.safe_dump(out, sys.stdout, sort_keys=False, width=140)


if __name__ == "__main__":
    main()
