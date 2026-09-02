# B200 manifests

Discrete B200 is intentionally separate from GB200. Do not copy the GB200
profile blindly: it assumes Grace arm64 nodes, an NVLink compute-domain claim,
MNNVL, four GPUs per node, and OCI-specific storage/fabric paths.

The `gke-b200` profile records the live GKE A4 cluster: eight B200 GPUs and
eight RoCE interfaces per amd64 node, node-local SSD cache, and no shared
workspace PVC. The PCP8/DCP8 target overlays the exact Python-only direct-KV
patch stack on the cluster's proven vLLM 0.27 image at startup.

```text
glm-5.2/b200/p1-pcp8dcp8ep-d1-dp8ep-dspark
```
