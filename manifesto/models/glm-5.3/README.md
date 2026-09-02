# GLM-5.3 P/D matrix

Each accelerator directory has the same five-file layout:

```text
base-frankenstein.yaml
base-agentx-prefill8-dp8.yaml
p1-pcp8ep-d1-dp8ep-mtp-agentx.yaml
p1-pcp8dcp8ep-d1-dp8ep-mtp-a2a-agentx.yaml
p1-tp8ep-d1-dp8ep-mtp-agentx.yaml
```

The three concrete arms compare PCP8+EP8, PCP8+DCP8+EP8 A2A, and TP8+EP8.
Every arm uses the platform's same shared DP8+EP8 decoder, MTP3 on both roles,
prefix caching, no model-weight CPU offload, and no `--enforce-eager`.

- `b200/` and `h200/` use one eight-GPU node per role.
- `gb200/` uses two four-GPU nodes per role in one NVLink compute domain.

The platform base owns the model revision, backend, `ve` path, and exact vLLM
commit. Update the base once when changing builds; do not pin different worker
commits in individual arms. Before benchmarking, render the selected arm and
record its resolved server arguments and vLLM build information.
