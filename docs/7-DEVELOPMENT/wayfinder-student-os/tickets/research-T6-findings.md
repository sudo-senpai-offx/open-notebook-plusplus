# Research T6 findings — LightGBM + FLAML tabular build impact

Parent ticket: `docs/7-DEVELOPMENT/wayfinder-student-os/tickets/T6-tabular-build-impact.md`.
Scope: Pentium D SSE2-only x86_64 / ARMv8-A Docker images, 2 GB RAM envelope, `max-tasks=1` worker.
Date: 2026-10-11. All claims cite primary sources; estimates are labelled as such.
Measurements were taken on x86_64 (CPython 3.12, numpy 2.5.3, scipy 1.18.1,
scikit-learn 1.9.1, pandas 2.3.3) using released wheels/sdist sources, not memory.

## 1. Verdicts

- **LightGBM: wheel OK on both arches. No from-source build needed.**
  PyPI ships `manylinux_2_27/2_28_x86_64` (3.5 MB) and `manylinux2014_aarch64`
  (3.3 MB) wheels for 4.7.0, plus sdist; `requires-python >= 3.10` covers the
  repo's `>=3.11,<3.13`.
  Source: https://pypi.org/project/lightgbm/ (files table for 4.7.0)
- **SSE2-only Pentium D: wheel OK.** Three independent evidences: (a) the root
  `CMakeLists.txt` sets no `-march`/`-mavx*` flags — only `-O3 -funroll-loops
  -fPIC`, and its only arch probes are SSE-era `_mm_prefetch`/`_mm_malloc`
  behind compile checks; (b) CI builds release wheels with stock gcc in
  `manylinux_2_28_x86_64` / `manylinux2014_aarch64` containers with no extra
  arch flags; (c) measured: `objdump -d` on the shipped 4.6.0
  `lib_lightgbm.so` (10 MB) shows **zero** VEX-encoded (AVX) mnemonics and
  **zero** `%ymm`/`%zmm` register uses. Plain `x86_64` tag (not `x86_64_v2/v3`)
  means baseline x86-64 (SSE2). Upstream FAQ also states the wheels are built
  to be "compatible with any hardware".
  Sources: https://raw.githubusercontent.com/lightgbm-org/LightGBM/main/CMakeLists.txt ;
  https://raw.githubusercontent.com/lightgbm-org/LightGBM/main/.github/workflows/python_package.yml ;
  https://lightgbm.readthedocs.io/en/latest/Installation-Guide.html ;
  https://raw.githubusercontent.com/lightgbm-org/LightGBM/main/docs/FAQ.rst (Python Q3)
- **ARMv8-A NEON: wheel OK.** `manylinux2014_aarch64` (glibc 2.17+) wheel exists
  and CI builds/tests it with stock gcc (`test-linux-aarch64` job), i.e. an
  ARMv8-A baseline build with no special flags.
  Sources: https://pypi.org/project/lightgbm/ ;
  https://raw.githubusercontent.com/lightgbm-org/LightGBM/main/.github/workflows/python_package.yml
- **FLAML: pure-Python, light base — but `flaml[automl]` hard-requires xgboost
  at import time** (see §3). `requires-python >= 3.10`.
  Source: https://raw.githubusercontent.com/microsoft/FLAML/main/pyproject.toml ;
  https://microsoft.github.io/FLAML/docs/Installation/

## 2. LightGBM dependency weight (small)

- Base install deps are only `narwhals>=1.15`, `numpy>=1.21.3`, `scipy`
  (pandas / scikit-learn / dask / pyarrow are optional extras).
  Source: https://raw.githubusercontent.com/lightgbm-org/LightGBM/main/python-package/pyproject.toml
- `import lightgbm` dlopen's `lib_lightgbm` **immediately at first import**
  (`basic.py` header: `from .libpath import _LIB`, "intentionally done here, as
  early as possible" to dodge the aarch64 `libgomp` static-TLS failure) and
  registers the native log callback; it also unconditionally imports
  narwhals/numpy/scipy. sklearn/dask/plotting submodules are `try/except`-guarded.
  Source: https://raw.githubusercontent.com/lightgbm-org/LightGBM/main/python-package/lightgbm/basic.py ;
  https://raw.githubusercontent.com/lightgbm-org/LightGBM/main/python-package/lightgbm/__init__.py ;
  https://raw.githubusercontent.com/lightgbm-org/LightGBM/main/docs/FAQ.rst (Python Q17, TLS block)
- From-source fallback (only if a wheel ever goes missing): `cmake` (root
  `CMakeLists.txt` requires **>= 3.28**), C++17 compiler, and on Linux just
  `cmake -B build -S . && cmake --build build -j4` (gcc) or
  `pip install lightgbm --no-binary lightgbm` (build backend
  `scikit-build-core`, fetched at build time unless `--no-isolation` with
  preinstalled tools). The backend-builder stage already has `build-essential`.
  No ISA flags needed for SSE2.
  Sources: https://lightgbm.readthedocs.io/en/latest/Installation-Guide.html ;
  https://github.com/lightgbm-org/LightGBM/tree/main/python-package (build-from-sources section) ;
  repo `Dockerfile:34-36`

## 3. FLAML weight + the xgboost hard dependency (assumption breaker)

- `FLAML` base wheel is ~0.3–0.4 MB pure-Python depending only on `NumPy>=1.17`.
  `import flaml` alone works without automl deps (measured: prints the
  "flaml.automl is not available" warning and continues).
  Source: https://raw.githubusercontent.com/microsoft/FLAML/main/pyproject.toml ;
  https://raw.githubusercontent.com/microsoft/FLAML/main/flaml/__init__.py
- The `[automl]` extra declares `lightgbm>=2.3.1`, `xgboost>=0.90,<3.0.0`,
  `scipy>=1.4.1`, `pandas>=1.1.4`, `scikit-learn>=1.0.0`.
  Source: https://raw.githubusercontent.com/microsoft/FLAML/main/pyproject.toml
- **But xgboost is not merely an install dep: it is an unguarded top-level
  import.** `flaml/automl/model.py` line 26 (v2.3.0 tag) / 27 (main) does
  `from xgboost import __version__ as xgboost_version` outside any
  `try/except`, and sklearn imports (lines 20–26) are likewise unguarded.
  Only the lightgbm import is guarded (`try/except ImportError → None`).
  Measured: `from flaml.automl import AutoML` without xgboost installed fails
  with `ModuleNotFoundError: No module named 'xgboost'`.
  Sources: https://raw.githubusercontent.com/microsoft/FLAML/main/flaml/automl/model.py ;
  tag v2.3.0 same file, line 26
- Consequence: **xgboost must be installed even if `estimator_list` excludes
  every xgb estimator.** You cannot "just skip xgboost". (If that cost is
  unacceptable, the fallback is dropping FLAML and driving LightGBM +
  sklearn directly with a hand-rolled search.)

## 4. The xgboost wheel trap (image-size critical)

- xgboost 2.1.4 (newest release satisfying FLAML's `<3.0.0` pin) publishes
  **two** x86_64 wheels: `manylinux2014_x86_64` = **4.5 MB** vs
  `manylinux_2_28_x86_64` = **223.6 MB**. Upstream's install docs confirm
  default wheels bundle GPU/CUDA support and only the separate `xgboost-cpu`
  variant is small — but `xgboost-cpu` is a 3.x-only package, unusable under
  FLAML's `<3.0.0` pin. aarch64 wheels are both small (4.2/4.4 MB).
  Sources: https://pypi.org/pypi/xgboost/2.1.4/json (file table) ;
  https://xgboost.readthedocs.io/en/stable/install.html ("Minimal installation (CPU-only)")
- On `python:3.12-slim-trixie` (glibc ≥ 2.28) **both** x86_64 tags are
  compatible and pip prefers the newer `manylinux_2_28` tag, i.e. a naive
  `xgboost==2.1.4` resolves to the **223 MB CUDA wheel**. Mitigation: URL-pin
  the `manylinux2014` wheel per arch (see §7), and check the resolved filename
  in `uv.lock`.
- Wheel-weight table (cp312 x86_64; aarch64 within ~5% except xgboost, see above):
  lightgbm 3.5 + FLAML 0.4 + narwhals 0.5 + scikit-learn 9.2 + pandas ~10.8 +
  scipy 35.3 + xgboost 4.5 (pinned) + joblib/threadpoolctl/dateutil/tzdata ~2
  ≈ **~66 MB of wheels. ESTIMATE: +150–250 MB installed** (native wheels
  unpack larger); **+400 MB if the CUDA xgboost wheel slips in.**
  Sources: per-package PyPI JSON file tables, e.g.
  https://pypi.org/pypi/lightgbm/4.7.0/json , https://pypi.org/pypi/scipy/1.18.1/json

## 5. Import-time RAM (measured) and the lazy-import seam

Peak RSS (`ru_maxrss`, measured, see §1 env):

| import statement | peak RSS |
|---|---|
| fresh python | 11 MB |
| `+ numpy + scipy.sparse + narwhals` | 56 MB |
| `+ lightgbm` | 171–173 MB (identical with `OMP_NUM_THREADS=1`) |
| `+ flaml` (automl unavailable) | 184 MB |
| `import sklearn + pandas` (separate process) | 145 MB |

- Full `flaml.automl` stack was not measurable here (no xgboost) but is bounded
  below by sklearn+pandas+lightgbm ≈ **250–300 MB+ peak per importing process**.
  Against the 2 GB envelope that is affordable **only** when paid per tabular
  task, never at worker boot.
- Seam (already the plan — confirmed correct and necessary): the worker boots
  with `surreal-commands-worker --import-modules commands --max-tasks
  ${OPEN_NOTEBOOK_WORKER_MAX_TASKS:-1}` (repo `supervisord.conf:22-25`), so a
  top-level `import lightgbm` / `import flaml.automl` anywhere in `commands/`
  taxes **every** worker boot and every non-tabular task. Keep all three
  imports **function-local** in the tabular handler(s); `import flaml` alone is
  cheap but buys nothing without `flaml.automl`. Suggested follow-up (not done
  here — research only): a test asserting `lightgbm`/`flaml.automl` absent from
  `sys.modules` after `import commands`.
- Synergy with `max-tasks=1`: the child process is replaced after each task, so
  the lazy import's RSS is reclaimed between tasks — no slow leak accumulation.

## 6. Runtime RAM guidance for the 2 GB envelope

- `AutoML` defaults that must be overridden (from the official API reference):
  `n_jobs` defaults to **-1 (all cores)** → set `n_jobs=1`;
  `estimator_list` defaults to `'auto'` → pass an explicit small list, e.g.
  `['lgbm', 'rf', 'extra_tree']` (xgb estimators stay unselected but xgboost
  must still be installed, §3); keep `n_concurrent_trials=1` (default, needs
  ray/spark otherwise); keep `model_history=False` (default);
  prefer `eval_method='holdout'` over CV to roughly halve peak training RAM;
  bound the search with `time_budget` and `mem_thres` (bytes).
  Source: https://microsoft.github.io/FLAML/docs/reference/automl/automl/
- LightGBM-side caps: `num_threads=1` (OpenMP) and `histogram_pool_size` (MB)
  bound native RAM (FAQ Python Q3 covers OOM tuning).
  Source: https://raw.githubusercontent.com/lightgbm-org/LightGBM/main/docs/FAQ.rst
- Fork-safety caveat: upstream documents LightGBM+OpenMP hanging when combined
  with `fork()` (FAQ Q11 — AWS Batch array jobs precedent). With single-thread
  settings (`n_jobs=1`, one trial at a time) this risk is minimised, but the
  tabular task should be soak-tested under the real worker start method before
  relying on it.
  Source: https://raw.githubusercontent.com/lightgbm-org/LightGBM/main/docs/FAQ.rst (Q11)

## 7. Recommended pins and offline-vendoring recipe

Suggested `pyproject.toml` addition (repo uses `uv.lock`, Dockerfile copies
both — repo `Dockerfile:55`, `UV_LINK_MODE=copy` at `Dockerfile:47`):

```toml
flaml==2.7.0
lightgbm==4.6.0        # 4.6.0 import-verified here under numpy 2.5.3; 4.7.0 latest also py>=3.10
scikit-learn>=1.5      # floor keeps resolver off pre-numpy-2 releases (repo pins numpy>=2.4.1)
pandas>=2.2
scipy>=1.13            # resolver picks 1.15.x on 3.11 / 1.18.x on 3.12
# xgboost: FLAML requires <3.0.0; URL-pin the CPU (manylinux2014) wheel PER ARCH
# so the 223 MB manylinux_2_28 CUDA wheel can never resolve on glibc>=2.28:
#   x86_64:  https://files.pythonhosted.org/.../xgboost-2.1.4-py3-none-manylinux2014_x86_64.whl
#   aarch64: https://files.pythonhosted.org/.../xgboost-2.1.4-py3-none-manylinux2014_aarch64.whl
# (exact URLs/filenames from https://pypi.org/pypi/xgboost/2.1.4/json;
#  gate with `platform_machine` markers or per-arch requirements files)
```

then `uv lock` and verify the locked xgboost filename contains `manylinux2014`.

- Offline vendoring: `pip download` performs the same resolution as install
  and collects distributions into a directory servable later via
  `pip install --no-index --find-links`; `--platform/--python-version/
  --implementation/--abi` (+ `--only-binary=:all:`) cross-fetch for another
  interpreter — vendor **one directory per (arch, cp312) triple**
  (`manylinux_2_28_x86_64` + `manylinux2014_aarch64` at minimum) since this
  repo builds multi-arch images (Dockerfile notes a QEMU arm64 leg).
  Source: https://pip.pypa.io/en/stable/cli/pip_download/
- From-source builds are **not** needed for either package on either arch, so
  the portable build needs no cmake toolchain beyond what `Dockerfile` already
  has; vendored wheels + `--no-index --find-links` suffice.

## 8. Assumptions contradicted

1. "FLAML pulls sklearn/pandas/numpy at minimum" — incomplete: `flaml[automl]`
   additionally **requires xgboost installed to import at all** (unguarded
   import, §3), and FLAML's own `<3.0.0` pin collides with xgboost's packaging
   split so the naive resolve costs **223 MB** on modern glibc (§4).
2. "SSE2 fallback build may be needed" — refuted: stock LightGBM wheels are
   already baseline-ISA on both arches (binary-verified, §1). Nothing to compile.
3. "Worker pays ~nothing when tabular is unused" — true **only** with
   function-local imports: a top-level import costs ~170–300 MB peak RSS per
   worker process (measured, §5). `max-tasks=1` makes the lazy cost
   self-cleaning between tasks.
4. No contradiction on "storage is cheap": ~66 MB of wheels is negligible; the
   binding constraints are import-time RAM (lazy seam) and the xgboost variant
   pin — both handled above.
