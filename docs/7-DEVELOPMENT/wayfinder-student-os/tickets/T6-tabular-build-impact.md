# T6 — Tabular Build Impact (LightGBM + FLAML)

Label: `wayfinder:research` (AFK). Parent: `../map.md`. Blocks: nothing.
Blocked by: nothing (frontier).

## Resolution (2026-10-11)

RESEARCHED — findings in `research-T6-findings.md` (same folder). Verdicts:
(1) LightGBM wheels OK both arches, SSE2-safe (objdump-verified, no AVX), no
build needed; (2) FLAML automl **hard-requires xgboost at import**
(unguarded) — xgboost must be installed even when unused; (3) xgboost wheel
trap: naive pin resolves the 223MB CUDA wheel on trixie glibc — URL-pin the
manylinux2014 wheel per arch (~66MB wheels, ~150–250MB installed);
(4) import RSS ~250–300MB/process ⇒ function-local lazy imports mandatory
(max-tasks=1 makes it self-cleaning); (5) runtime knobs pinned
(n_jobs=1, explicit estimator_list, holdout, mem_thres, num_threads=1).
Dependency addition is a later implementation slice, not this ticket.

## Question

Against primary sources (LightGBM / FLAML install docs, SSE2/AVX dispatch
notes, ARM wheels), determine the cost of adding both to the Docker image:
native-build requirements for SSE2-only x86 and ARMv8-A NEON, wheel
availability vs from-source compile, image-size delta, import-time RAM delta
in the worker (must not regress the 2GB envelope at import — lazy-load behind
function imports if needed), and offline-wheel vendoring options for the
future portable build. Record exact version pins + the lazy-import seam so the
worker pays nothing when tabular is unused.
