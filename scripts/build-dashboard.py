#!/usr/bin/env python3
"""Build a sweep's interactivity_vs_throughput.html under results/.artifacts/.

The sweep layout (results_<config>/results_<config>_c<N>/profile_export_aiperf.json) is
exactly what gen_interactivity_chart.py expects, so this is a thin wrapper that checks the
input and forwards MODEL_LABEL from model_label.txt when present.
"""
import os
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else "results")
profiles = sorted(root.glob("results_*/results_*_c*/profile_export_aiperf.json"))
if not profiles:
    raise SystemExit(f"No results_<config>/results_<config>_c<N>/profile_export_aiperf.json under {root}; run 'just results'")
env = os.environ.copy()
labels = {p.read_text().strip() for p in root.glob("results_*/model_label.txt") if p.read_text().strip()}
if len(labels) == 1 and "MODEL_LABEL" not in env:
    env["MODEL_LABEL"] = labels.pop()
subprocess.run([sys.executable, "gen_interactivity_chart.py", str(root)], check=True, env=env)
print(root / "interactivity_vs_throughput.html")
