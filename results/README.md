# Local benchmark results

Generated benchmark outputs, logs, plots, datasets, and dependency caches live
under `results/.artifacts/`. That directory is intentionally gitignored.

Reproduction scripts and deployment configuration belong in `reproductions/`,
`scripts/`, `manifesto/`, or `deploy/`, not in the artifact directory.

The local retention policy is 24 hours unless a result is deliberately exported
elsewhere. The cleanup performed on 2026-09-02 removed 71 expired artifact
paths (42.5 GB). Shared script defaults now write generated output beneath this
directory.
