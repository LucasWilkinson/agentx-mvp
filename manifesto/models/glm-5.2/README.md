# GLM-5.2 config

The hardware-specific target configs are:

```text
glm-5.2/h200/p1-pcp8dcp8ep-d1-dp8ep-dspark
glm-5.2/b200/p1-pcp8dcp8ep-d1-dp8ep-dspark-agentx
glm-5.2/b200/p1-tp8ep-d1-dp8ep-dspark-agentx
glm-5.2/gb200/p1-pcp8dcp8ep-d1-dp8ep-dspark-agentx
glm-5.2/gb200/p1-tp8ep-d1-dp8ep-dspark-agentx
glm-5.2/gb200/p1-pcp4dcp4ep-d1-dp4ep-dspark
glm-5.2/gb200/p1-dp2pcp4dcp4ep-d1-dp8ep-dspark
```

The full-size configs run `zai-org/GLM-5.2-FP8` with DP8 + EP decode and
DSpark. H200 uses PCP8 x DCP8 on one 8-GPU prefill node. The experimental
full-size GB200 variant uses DP2 x PCP4 x DCP4 + EP8, keeping PCP node-local
while expert weights span two 4-GPU workers in the GB200 NVLink compute
domain. It takes its arm64/CUDA 13 build from `.env.gb200`.

The two B200 AgentX targets are a controlled P/D prefiller comparison. Both
use the same 8-GPU DP8/EP8 DSpark decoder and identical 142K serving limits;
only the 8-GPU prefiller changes between PCP8/DCP8/EP8 and TP8/EP8. Use the
ignored `.env.b200` cluster environment and a portable configuration selector
so its teardown selector cannot match another session's model servers.

The capacity-friendly GB200 target uses PCP4/DCP4 prefill and DP4/EP4 decode
on two four-GPU nodes. It has been smoke-tested through the standalone router
with the `ve` fix recorded in `gb200/dspark-kv-layout.patch`; see
`env-readme.md` for the application command.

The GB200 comparison targets span each 8-GPU prefiller and DP8/EP8 decoder
across pairs of four-GPU nodes in one NVLink compute domain. The PCP arm uses
a pod-local Python overlay for the f711ee1 multi-node PCP argument fix; it does
not modify the shared `ve` worktree. Use the single ignored `.env.gb200`
cluster environment and set the comparison's owner/router identity in the
launch command.

On H200, model load, full-length KV allocation, and PIECEWISE capture pass. A
short live request currently exposes a peer-KV fence bug when fewer than eight
PCP ranks have real tokens. GB200 initially retains the proven scheduler
and memory limits; tune them after its first smoke run.

The unsuffixed `p1-pcp8dcp8ep-d1-dp8ep-dspark` remains as a compatibility
target for the in-flight CoreWeave session.

The retained comparison configs are:

- `h200/p1-dp8ep-d1-dp8ep-dspark`: correctness baseline;
- `h200/p1-pcp8ep-d1-dp8ep-dspark`: replicated-PCP fallback.

The H200 configs use `/workspace/vdptest/frankenstein-prefiller-lucas-dcp` where the
branch-specific direct PCP/NIXL and PCP-spanning DCP support is required.

`base.yaml` contains the general GLM defaults. `base-frankenstein.yaml` adds
the shared direct-NIXL, DSpark, and backend settings. The abstract
`h200/base-frankenstein.yaml`, `b200/base-frankenstein.yaml`, and
`gb200/base-frankenstein.yaml` add accelerator guards and fabric/topology
specialization. The hardware targets remain in separate directories.

The decoder is capped at 114688 tokens for its current KV capacity.
