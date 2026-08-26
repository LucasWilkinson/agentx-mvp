#!/usr/bin/env bash
# Accuracy smoke check against the currently served model: lm_eval (local-completions) on the devbox,
# through the same router URL the benchmarks use. Usage: accuracy-check.sh [out-dir]
# Env: ACCURACY_TASKS (default gsm8k), ACCURACY_LIMIT (default 200 samples/task), ACCURACY_CONCURRENCY (8).
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh; load_agentx_env; require_agentx_env KUBE_CONTEXT NAMESPACE URL MODEL
out_dir="${1:-results/accuracy/$(date +%Y%m%dT%H%M%SZ)}"; mkdir -p "$out_dir"
tasks="${ACCURACY_TASKS:-gsm8k}"; limit="${ACCURACY_LIMIT:-200}"; conc="${ACCURACY_CONCURRENCY:-8}"
devbox_env="${ACCURACY_DEVBOX_ENV:-/workspace/vdptest/glm53-prefiller}"
remote_dir="/tmp/accuracy-$$"
echo "accuracy: tasks=$tasks limit=$limit concurrency=$conc url=$URL model=$MODEL -> $out_dir"
k exec devbox -- bash -c "
set -euo pipefail; source '$devbox_env/.venv/bin/activate'; mkdir -p '$remote_dir'
export HF_HOME=/models/hf HF_HUB_OFFLINE=0
lm_eval --model local-completions --tasks '$tasks' --num_fewshot 5 --limit '$limit' --batch_size 1 \
  --model_args 'base_url=$URL/v1/completions,model=$MODEL,num_concurrent=$conc,max_retries=3,tokenized_requests=False,timeout=1800' \
  --gen_kwargs 'temperature=0' --output_path '$remote_dir' --log_samples 2>&1 | tail -25"
k cp "devbox:$remote_dir" "$out_dir" >/dev/null
k exec devbox -- rm -rf "$remote_dir"
python3 - "$out_dir" <<'PY'
import glob, json, sys
for f in sorted(glob.glob(f"{sys.argv[1]}/**/results_*.json", recursive=True)):
    r = json.load(open(f))
    for task, m in r["results"].items():
        keys = {k: v for k, v in m.items() if isinstance(v, (int, float)) and "stderr" not in k and k != "alias"}
        print(f"{task}: " + ", ".join(f"{k}={v:.3f}" for k, v in keys.items()))
PY
