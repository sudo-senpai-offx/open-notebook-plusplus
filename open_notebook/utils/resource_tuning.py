"""Resource tuning for low-end CPU/RAM environments.

Applies conservative thread settings to avoid oversubscription on weak CPUs
and sets tokenizer parallelism to avoid thread contention. Safe to call
multiple times (idempotent).
"""

import os

_ENV_VARS = {
    "TOKENIZERS_PARALLELISM": "false",
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
}

_TRIED_TORCH = False


def apply_tuning() -> None:
    """Apply resource tuning defaults."""
    for k, v in _ENV_VARS.items():
        if k not in os.environ:
            os.environ[k] = v

    global _TRIED_TORCH
    if _TRIED_TORCH:
        return
    _TRIED_TORCH = True
    try:
        import torch

        try:
            torch.set_num_threads(1)
        except Exception:
            pass
        try:
            torch.set_num_interop_threads(1)
        except Exception:
            pass
    except Exception:
        # torch may not be installed in some contexts
        pass
