"""T6: tabular stack must stay out of the worker's import-time footprint.

lightgbm/sklearn/pandas/scipy are resolved dependencies (tabular training),
but every top-level import of them in `commands/` or graph modules taxes
every worker boot and every non-tabular task. They stay function-local in
tabular handlers; this test locks the seam.
"""

from __future__ import annotations

TABULAR_HEAVY = ("lightgbm", "sklearn", "pandas", "scipy", "flaml", "xgboost")


def test_worker_imports_stay_light():
    import sys

    import commands  # noqa: F401
    import open_notebook.graphs.source  # noqa: F401
    import open_notebook.graphs.transformation  # noqa: F401

    resident = [m for m in TABULAR_HEAVY if m in sys.modules]
    assert resident == [], f"heavy tabular modules imported at boot: {resident}"
