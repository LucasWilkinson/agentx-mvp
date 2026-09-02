# Shared vLLM development environment

The PR8 image supplies CUDA, compilers, and `ve`. The vLLM checkout, Python
environment, build products, and caches live on a shared PVC so every model pod
can use the same absolute path.

## 1. Create the workspace PVC

This is the CoreWeave H200 PVC used by this repository. `shared-vast` is
cluster-specific; use an equivalent `ReadWriteMany` storage class elsewhere.

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: workspace-lwilkinson
  namespace: lwilkinson-dev
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: shared-vast
  resources:
    requests:
      storage: 500Gi
```

```bash
kubectl apply -f workspace-pvc.yaml
kubectl -n lwilkinson-dev wait pvc/workspace-lwilkinson \
  --for=jsonpath='{.status.phase}'=Bound --timeout=5m
```

## 2. Start a devbox using the PR8 image

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: vllm-devbox
  namespace: lwilkinson-dev
spec:
  containers:
    - name: devbox
      image: quay.io/rh-ee-lwilkins/vllm-envs-cuda:pr8-cv2
      command: [bash, -lc, "sleep infinity"]
      volumeMounts:
        - name: workspace
          mountPath: /workspace
  volumes:
    - name: workspace
      persistentVolumeClaim:
        claimName: workspace-lwilkinson
```

```bash
kubectl apply -f vllm-devbox.yaml
kubectl -n lwilkinson-dev wait pod/vllm-devbox \
  --for=condition=Ready --timeout=10m
kubectl -n lwilkinson-dev exec -it vllm-devbox -- bash
```

The image is private. Configure the namespace's service account or add an
`imagePullSecrets` entry if it is not already authorized to pull from
`quay.io/rh-ee-lwilkins`.

## 3. Create a vLLM environment with `ve`

Run these commands inside the devbox:

```bash
mkdir -p /workspace/vdptest /workspace/.cache/vllm-envs
git clone https://github.com/vllm-project/vllm.git \
  /workspace/vdptest/vllm-main
git -C /workspace/vdptest/vllm-main remote add lucas \
  https://github.com/LucasWilkinson/vllm.git
git -C /workspace/vdptest/vllm-main fetch lucas \
  frankenstein-prefiller-lucas-dcp

export VE_CACHE_DIR=/workspace/.cache/vllm-envs
export VE_ENVS_ROOT=/workspace/vdptest
# The devbox has no GPU, so tell ve which extensions to build for H200.
export TORCH_CUDA_ARCH_LIST=9.0a

ve new lucas/frankenstein-prefiller-lucas-dcp \
  --name frankenstein-prefiller-lucas-dcp \
  --repo /workspace/vdptest/vllm-main
```

`ve new` creates both the worktree and its `.venv` under
`/workspace/vdptest/frankenstein-prefiller-lucas-dcp`. Fetch first: `ve new`
uses the local remote-tracking ref and does not fetch it automatically.
Keep `TORCH_CUDA_ARCH_LIST=9.0a` set for `ve status`, `ve sync`, and other
commands in a zero-GPU H200 devbox; otherwise `ve` cannot query `nvidia-smi`.

Useful checks:

```bash
ve list
cd /workspace/vdptest/frankenstein-prefiller-lucas-dcp
ve status
.venv/bin/python -c 'import vllm; print(vllm.__version__)'
git log -1 --oneline
```

To update a clean existing environment:

```bash
git -C /workspace/vdptest/vllm-main fetch lucas \
  frankenstein-prefiller-lucas-dcp
cd /workspace/vdptest/frankenstein-prefiller-lucas-dcp
git switch --detach lucas/frankenstein-prefiller-lucas-dcp
ve sync
```

Do not switch or sync a worktree with uncommitted changes. Create another
named environment instead.

## 4. Use the environment from AgentX/Manifesto

For one shared vLLM build, set:

```dotenv
VLLM_ENV=/workspace/vdptest/frankenstein-prefiller-lucas-dcp
VLLM_IMAGE=quay.io/rh-ee-lwilkins/vllm-envs-cuda:pr8-cv2
```

For a role-specific build, set the path on that role:

```yaml
model:
  image: quay.io/rh-ee-lwilkins/vllm-envs-cuda:pr8-cv2
roles:
  prefill:
    env:
      MANIFESTO_VLLM_ENV: /workspace/vdptest/frankenstein-prefiller-lucas-dcp
      PYTHONPATH: /workspace/vdptest/frankenstein-prefiller-lucas-dcp
```

The cluster definition must mount the same PVC at `/workspace`; this repository
does that in `manifesto/clusters/coreweave-h200.yaml`.

Verify the rendered command before deploying:

```bash
just envs
just args manifesto/models/glm-5.2/p1-pcp8dcp8ep-d1-dp8ep-dspark.yaml
just deploy manifesto/models/glm-5.2/p1-pcp8dcp8ep-d1-dp8ep-dspark.yaml
```

## GB200 (NVIDIA AI-Accelerator)

GB200 uses the existing `lustre-pvc-vllm` claim in the shared `vllm`
namespace. The repository's devbox mounts it at `/mnt/lustre` and always uses
the GB200 kube context explicitly:

```bash
just gb200-tunnel
just gb200-devbox up
just gb200-devbox shell
```

The persistent environment for this branch is:

```text
/mnt/lustre/lwilkinson/vdptest/frankenstein-prefiller-lucas-dcp
```

It uses `nvcr.io/nvidia/pytorch:25.08-py3`, Linux ARM64, CUDA 13, and
`TORCH_CUDA_ARCH_LIST=10.0+PTX`. On ARM64, omit vLLM's test dependency set
because `decord==0.6.0` has no ARM64 wheel:

```bash
cd /mnt/lustre/lwilkinson/vdptest/frankenstein-prefiller-lucas-dcp
VE_WITH_TEST=0 ve sync
```

If PyTorch selects CUDA 13.2 before FlashInfer publishes a `cu132` index, seed
the matching official CUDA 13.0 ARM64 JIT-cache wheel once, then rerun sync:

```bash
uv pip install --python .venv/bin/python \
  'flashinfer-jit-cache==0.6.17' --no-deps \
  --index-url https://flashinfer.ai/whl/cu130
VE_WITH_TEST=0 ve sync
```

The CUDA 13.0 ARM64 dependency layer can also contain `torchaudio` while a
newer `ve` resolution selects CUDA 13.2 PyTorch. Text-only vLLM does not need
TorchAudio; remove that incompatible optional package if `import vllm.config`
reports a CUDA 13.0/13.2 mismatch:

```bash
uv pip uninstall --python .venv/bin/python torchaudio
.venv/bin/python -c 'import vllm.config; print("vLLM import OK")'
```

Use the isolated GB200 dotenv file so the active CoreWeave work remains
untouched:

```bash
just --dotenv-filename .env.gb200 render
just --dotenv-filename .env.gb200 deploy
```

The validated capacity-friendly target is
`glm-5.2/gb200/p1-pcp4dcp4ep-d1-dp4ep-dspark` (one four-GPU prefiller and one
four-GPU decoder). `.env.gb200` uses router probe port `33081`; CoreWeave may
already own the default `33080` port.

Commit `f711ee1b38` copies DSpark's `CacheConfig` before the engine resolves
the target KV layout. Apply the recorded two-file fix to that clean `ve`
worktree before deploying:

```bash
kubectl --context default -n vllm exec -i lwilkinson-vllm-devbox -- \
  bash -lc 'cd /mnt/lustre/lwilkinson/vdptest/frankenstein-prefiller-lucas-dcp && git apply -' \
  < manifesto/models/glm-5.2/gb200/dspark-kv-layout.patch
```

This intentionally leaves the `ve` worktree dirty in those two files. It
synchronizes the draft cache to the resolved `LBHNC` layout before kernel
warmup; without it the decoder exits with `KV cache layout has not been
resolved yet`.
