# GLM-5.2 config

The active target config is:

```text
glm-5.2/p1-pcp8dcp8ep-d1-dp8ep-dspark
```

It runs `zai-org/GLM-5.2-FP8` with PCP8 x TP1 x DCP8 + EP prefill,
DP8 + EP decode, and DSpark. Model load, full-length KV allocation, and
PIECEWISE capture pass. A short live request currently exposes a peer-KV fence
bug when fewer than eight PCP ranks have real tokens.

The retained comparison configs are:

- `p1-dp8ep-d1-dp8ep-dspark`: correctness baseline;
- `p1-pcp8ep-d1-dp8ep-dspark`: replicated-PCP fallback.

All three use `/workspace/vdptest/frankenstein-prefiller-lucas-dcp` where the
branch-specific direct PCP/NIXL and PCP-spanning DCP support is required.

`base.yaml` contains the general GLM defaults. `base-frankenstein.yaml` adds
the shared direct-NIXL, DSpark, and backend settings. Both are abstract.

The decoder is capped at 114688 tokens for its current KV capacity.
