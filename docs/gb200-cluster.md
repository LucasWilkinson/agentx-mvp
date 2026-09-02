# GB200 cluster handoff

This target is separate from CoreWeave. Never rely on the active kube context:
all GB200 commands must include `--context default`. The commands and scripts
below do not call `kubectl config use-context` and do not modify `.env`.

## Access

| Setting | Value |
|---|---|
| Kubeconfig | Set `KUBECONFIG` in the ignored `.env.gb200` |
| Context | `default` |
| Namespace | `vllm` |
| API server | `https://10.0.0.39:6443` |
| Kubeconfig proxy | `socks5://localhost:1080` |
| Bastion | Set `GB200_BASTION` in the ignored `.env.gb200` |
| SSH user | Set `GB200_SSH_USER` in the ignored `.env.gb200` |
| SSH key | Set `GB200_SSH_KEY` in the ignored `.env.gb200` |
| Tailscale network | `nvfb.github` / `AI-Accelerator` |

Connect Tailscale to the NVIDIA AI-Accelerator network, then keep the tunnel
running in one terminal:

```bash
just gb200-tunnel
```

In another terminal, inspect node/GPU allocation and namespace workloads:

```bash
lsof -nP -iTCP:1080 -sTCP:LISTEN
just gb200-status
```

Equivalent direct read-only checks are:

```bash
kubectl --context default get nodes
kubectl --context default -n vllm get pods -o wide
```

`gb200-status` reports GPU capacity and allocated GPU requests separately.
An allocatable GPU count is node capacity, not current availability; subtract
the requested `nvidia.com/gpu` value shown beneath each node.

Verified inventory (2026-08-27): 42/42 nodes Ready, including 36 GPU nodes
with 144 allocatable GPUs total. This is cluster capacity, not necessarily the
number of currently free GPUs.

## Deployment profile

`manifesto/clusters/oci-gb200.yaml` records the live cluster inventory. To keep
the existing CoreWeave `.env` untouched, create a separate environment file:

```bash
cp .env.gb200.example .env.gb200
# Fill in the remaining REPLACE_ME values.
just --dotenv-filename .env.gb200 render
```

The GB200 environment selects the GB200-specific
`glm-5.2/gb200/p1-dp2pcp4dcp4ep-d1-dp8ep-dspark` spec. It has no embedded
CoreWeave `/workspace` build paths. Fill in GB200-compatible `VLLM_ENV` and
`VLLM_IMAGE` values before deployment; the nodes are Linux arm64 with CUDA 13,
so both must support that platform.
