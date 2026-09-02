# Repository instructions

## Generated results and local artifacts

- Write all generated benchmark results beneath `results/.artifacts/`.
- This includes raw result JSON/JSONL/CSV, benchmark databases, server and
  client logs, plots, profiler traces, downloaded datasets, cloned benchmark
  repositories, virtual environments, dependency caches, and image-build roots.
- Organize outputs by reproduction and run name, for example:
  `results/.artifacts/reproductions/<reproduction>/<run-name>/`.
- Never place generated output inside `reproductions/`, `manifesto/`, `scripts/`,
  `deploy/`, or the repository root.
- Keep portable inputs in the repository: scripts, manifests, patches, example
  environment files, exact commands, commit hashes, and concise Markdown
  findings belong with the relevant reproduction.
- Treat `results/.artifacts/` as ephemeral and gitignored. Do not make a
  reproduction depend on an artifact that exists only there.
- Local result retention is 24 hours unless the user explicitly requests that
  a result be preserved or exported.
