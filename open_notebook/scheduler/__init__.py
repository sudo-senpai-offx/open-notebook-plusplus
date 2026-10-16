"""Package marker for the admission scheduler.

Kept as a package with a docstring rather than a bare namespace so the modules
inside have an unambiguous home. `memory` and `footprint` are pure and have no
dependencies on the database or the API; `dispatcher` is the part that does.
"""
