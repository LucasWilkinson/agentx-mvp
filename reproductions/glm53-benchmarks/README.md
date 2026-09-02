# GLM-5.3 benchmark clients

This directory contains the two portable benchmark clients still used by the
repository:

- `lmsys-client.sh`: pinned LMSYS OpenHands workload for fast Pareto sweeps.
- `agentx.sh`: 142K-capped or full-context AgentX validation.

Both scripts write generated output beneath `results/.artifacts/` by default.
Server deployment is owned by the GLM-5.3 manifests and the sweep instructions
under `docs/`.

The removed legacy benchmark-image definition depended on a generated
`artifacts/super-image-root` tree and was therefore not a portable
reproduction. Dependency and dataset caches are instead generated beneath the
ignored artifact directory by `scripts/lmsys-run.sh`.
