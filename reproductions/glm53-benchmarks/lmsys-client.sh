#!/usr/bin/env bash
set -euo pipefail

# Portable reproduction of the client workload used by the LMSYS GLM
# optimization blog. The upstream source is pinned so two machines build and
# replay the same dataset. TOKENIZER_MODEL may differ from the API model alias.

: "${BASE_URL:?set BASE_URL, e.g. http://127.0.0.1:8000}"
: "${SERVED_MODEL:?set SERVED_MODEL to the name returned by /v1/models}"
: "${TOKENIZER_MODEL:?set TOKENIZER_MODEL, e.g. zai-org/GLM-5.3}"
: "${OUTPUT_DIR:?set OUTPUT_DIR}"
: "${RUN_NAME:?set RUN_NAME, e.g. tp8-none}"

BLOG_REPO=${BLOG_REPO:-https://github.com/Jiminator/sglang.git}
BLOG_COMMIT=${BLOG_COMMIT:-2bac7e166a7b5bf518b778817ec464cec0f75e3e}
BLOG_CHECKOUT=${BLOG_CHECKOUT:-$OUTPUT_DIR/lmsys-glm-blog-repro}
EVALSCOPE_COMMIT=${EVALSCOPE_COMMIT:-acd09b44384d53174768bb1063f675420f76fae9}
MODELSCOPE_VERSION=${MODELSCOPE_VERSION:-1.34.0}
LXML_VERSION=${LXML_VERSION:-6.0.2}
INSTALL_DEPS=${INSTALL_DEPS:-0}
LMSYS_NUMBERS=${LMSYS_NUMBERS:-"4 8 8 16"}
LMSYS_PARALLELS=${LMSYS_PARALLELS:-"1 2 4 8"}
LMSYS_DATASET_OFFSET=${LMSYS_DATASET_OFFSET:-0}
CLIENT_VENV=${CLIENT_VENV:-$OUTPUT_DIR/evalscope-venv-$EVALSCOPE_COMMIT}
CLIENT_PYTHON=${CLIENT_PYTHON:-python3.12}
DEPS_MARKER=$CLIENT_VENV/.lmsys-deps-$EVALSCOPE_COMMIT-$MODELSCOPE_VERSION-$LXML_VERSION.complete

# The cluster-wide HF cache may contain Arrow metadata written by a newer
# datasets release. Keep this pinned benchmark's dataset cache isolated.
export HF_DATASETS_CACHE=${HF_DATASETS_CACHE:-$OUTPUT_DIR/hf-datasets-cache-v3.6}
mkdir -p "$HF_DATASETS_CACHE"

if [[ ! -d $BLOG_CHECKOUT/.git ]]; then
  git clone --filter=blob:none "$BLOG_REPO" "$BLOG_CHECKOUT"
fi
git -C "$BLOG_CHECKOUT" fetch origin "$BLOG_COMMIT"
git -C "$BLOG_CHECKOUT" checkout --detach "$BLOG_COMMIT"

BLOG_DIR=$BLOG_CHECKOUT/benchmark/glm_nvfp4_blog
command -v "$CLIENT_PYTHON" >/dev/null 2>&1 || {
  echo "CLIENT_PYTHON=$CLIENT_PYTHON was not found; use Python 3.10-3.13" >&2
  exit 2
}
client_python_version=$("$CLIENT_PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
client_python_minor=${client_python_version#*.}
if [[ ${client_python_version%%.*} != 3 || $client_python_minor -lt 10 || $client_python_minor -ge 14 ]]; then
  echo "CLIENT_PYTHON must be Python 3.10-3.13; found $client_python_version" >&2
  exit 2
fi
venv_python_version=""
if [[ -x $CLIENT_VENV/bin/python ]]; then
  venv_python_version=$("$CLIENT_VENV/bin/python" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
fi
if [[ $venv_python_version != "$client_python_version" ]]; then
  [[ -z $venv_python_version ]] || echo "Recreating client venv: Python $venv_python_version -> $client_python_version"
  "$CLIENT_PYTHON" -m venv --clear "$CLIENT_VENV"
fi
source "$CLIENT_VENV/bin/activate"
needs_deps=0
if [[ $INSTALL_DEPS == 1 ]] && ! python3 -c \
  'import evalscope.perf.plugin.datasets.swe_smith' >/dev/null 2>&1; then
  needs_deps=1
fi
if [[ $needs_deps == 1 ]]; then
  # The pinned upstream file uses /usr/bin/bash, which is absent on macOS.
  # Invoke it through the caller's bash without modifying the checkout.
  bash "$BLOG_DIR/evalscope-deps/scripts/install_evalscope_deps.sh"
  pip install \
    "modelscope[datasets]==$MODELSCOPE_VERSION" \
    "lxml==$LXML_VERSION"
  # This client only needs EvalScope's performance extra. The broader `all`
  # extra pulls the optional vision stack and x86-only decord on ARM64.
  pip install \
    "evalscope[perf] @ git+https://github.com/modelscope/evalscope.git@$EVALSCOPE_COMMIT"
fi
python3 - <<'PY'
import evalscope.perf.plugin.datasets.swe_smith  # noqa: F401
PY
if [[ $INSTALL_DEPS == 1 ]]; then
  printf 'completed_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$DEPS_MARKER"
fi

slug=${TOKENIZER_MODEL//\//-}
dataset_key=$(printf '%s\n' \
  "$TOKENIZER_MODEL" "$BLOG_COMMIT" \
  'pad_source=openscience' 'first_turn_length=74160' \
  'subsequent_turn_length=753' 'num_turns=13' 'number=128' \
  | sha256sum | cut -c1-16)
dataset=${LMSYS_DATASET_PATH:-$BLOG_DIR/datasets/openhand-$slug-$dataset_key.json}
if [[ ! -s $dataset ]]; then
  dataset_tmp=$dataset.tmp.$$
  mkdir -p "$BLOG_DIR/datasets"
  python3 "$BLOG_DIR/build_openhands_padded_dataset.py" \
    --model "$TOKENIZER_MODEL" \
    --pad-source openscience \
    --first-turn-length 74160 \
    --subsequent-turn-length 753 \
    --num-turns 13 \
    --number 128 \
    --output-path "$dataset_tmp"
  mv "$dataset_tmp" "$dataset"
fi

server_ready() {
  # Direct vLLM endpoints expose /health. Some llm-d/EPP gateways return 503
  # there even while their OpenAI route is ready, but proxy /v1/models.
  curl -sf -m 3 "${BASE_URL%/}/health" >/dev/null 2>&1 \
    || curl -sf -m 3 "${BASE_URL%/}/v1/models" >/dev/null 2>&1
}
server_is_ready=0
for _ in $(seq 1 720); do
  if server_ready; then
    server_is_ready=1
    break
  fi
  sleep 5
done
(( server_is_ready == 1 )) || {
  echo "server did not become healthy at ${BASE_URL%/}" >&2
  exit 1
}

mkdir -p "$OUTPUT_DIR/$RUN_NAME"
{
  printf 'started_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'blog_commit=%s\n' "$BLOG_COMMIT"
  printf 'evalscope_commit=%s\n' "$EVALSCOPE_COMMIT"
  printf 'modelscope_version=%s\n' "$MODELSCOPE_VERSION"
  printf 'lxml_version=%s\n' "$LXML_VERSION"
  printf 'hf_datasets_cache=%s\n' "$HF_DATASETS_CACHE"
  printf 'client_venv=%s\n' "$CLIENT_VENV"
  printf 'base_url=%s\n' "$BASE_URL"
  printf 'served_model=%s\n' "$SERVED_MODEL"
  printf 'tokenizer_model=%s\n' "$TOKENIZER_MODEL"
  printf 'dataset=%s\n' "$dataset"
  printf 'workload=lmsys-openhands-glm-agentic\n'
  printf 'turns=13\n'
  printf 'first_turn_tokens=74160\n'
  printf 'subsequent_turn_tokens=753\n'
  printf 'max_output_tokens=220\n'
  printf 'numbers=%s\n' "$LMSYS_NUMBERS"
  printf 'parallels=%s\n' "$LMSYS_PARALLELS"
  printf 'dataset_offset=%s\n' "$LMSYS_DATASET_OFFSET"
} >"$OUTPUT_DIR/$RUN_NAME/reproduction.env"

cmd=(
  evalscope perf
  --model "$SERVED_MODEL"
  --url "${BASE_URL%/}/v1/chat/completions"
  --api openai
  --dataset swe_smith
  --dataset-path "$dataset"
  --dataset-offset "$LMSYS_DATASET_OFFSET"
  --max-tokens 220
  --multi-turn
  --number $LMSYS_NUMBERS
  --parallel $LMSYS_PARALLELS
  --extra-args '{"ignore_eos": true}'
  --name "$RUN_NAME"
  --outputs-dir "$OUTPUT_DIR"
  --no-timestamp
)
printf '%q ' "${cmd[@]}" >"$OUTPUT_DIR/$RUN_NAME/client-command.txt"
printf '\n' >>"$OUTPUT_DIR/$RUN_NAME/client-command.txt"
"${cmd[@]}"
printf 'finished_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  >>"$OUTPUT_DIR/$RUN_NAME/reproduction.env"
