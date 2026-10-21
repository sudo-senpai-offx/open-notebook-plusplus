"""Run the transformation as it was before the map-reduce, and save what it gave.

The version before the map-reduce handed the model a source's entire text in
one message. The model's window is smaller than the document, the provider
truncated it silently, and the result was presented as covering the whole
thing. The output looked entirely reasonable - which is what makes it the
worst kind of failure.

This runs that code, unchanged, against a source, so the comparison is like
for like: the same document, the same transformation, the same model, the
same hardware. The old version is taken out of the repository's history
rather than copied into a file, so what it runs is the code that actually
shipped.

    python scripts/insight-ab.py "21 Days" "Dense Summary" /tmp/old.txt

Then score both answers with scripts/insight-coverage.py to see which parts
of the document each one could have been about.

The old code is run from a temporary copy, and the answer is written to a
file rather than to the database: this is a measurement, not an insight, and
it must not land in the student's notes beside the real one.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from open_notebook.database.repository import repo_query

REPO = Path(__file__).resolve().parent.parent

#: The commit before the map-reduce went in.
DEFAULT_REF = "3fde419^"


class _Transformation:
    """The two fields the old code reads."""

    def __init__(self, prompt: str, title: str):
        self.prompt = prompt
        self.title = title


def _old_module(ref: str, path: Path, old_code: str | None = None) -> Any:
    if old_code:
        # The old code was extracted elsewhere - the container has no git, and
        # the code is the same either way.
        source = Path(old_code).read_text(encoding="utf-8")
    else:
        source = subprocess.run(
            [
                "git",
                "-C",
                str(REPO),
                "show",
                f"{ref}:open_notebook/graphs/transformation.py",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("old_transformation", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["old_transformation"] = module
    spec.loader.exec_module(module)
    return module


async def run(
    source_title: str,
    transformation_title: str,
    out_path: str,
    ref: str,
    old_code: str | None = None,
) -> int:
    with tempfile.TemporaryDirectory() as directory:
        old = _old_module(ref, Path(directory) / "old_transformation.py", old_code)

        sources = (
            await repo_query(
                "SELECT id, title, full_text FROM source WHERE title CONTAINS $t",
                {"t": source_title},
            )
            or []
        )
        if not sources:
            print(f"no source matching {source_title!r}")
            return 1
        source = sources[0]

        transformations = (
            await repo_query(
                "SELECT prompt, title FROM transformation WHERE title = $t",
                {"t": transformation_title},
            )
            or []
        )
        if not transformations:
            print(f"no transformation titled {transformation_title!r}")
            return 1
        transformation = _Transformation(
            transformations[0]["prompt"], transformations[0]["title"]
        )

        state = {
            "source": None,
            "input_text": source["full_text"],
            "transformation": transformation,
        }
        result = await old.run_transformation(state, config={"configurable": {}})

    text = result["output"]
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(text)
    print(f"wrote {len(text)} characters to {out_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="part of the source's title")
    parser.add_argument("transformation", help="the transformation's title")
    parser.add_argument("out", help="where to write the answer")
    parser.add_argument(
        "--ref",
        default=DEFAULT_REF,
        help=f"the git revision to take the old code from (default: {DEFAULT_REF})",
    )
    parser.add_argument(
        "--old-code",
        help=(
            "a file holding the old code, for when there is no git to take it "
            "from - the container, for one"
        ),
    )
    args = parser.parse_args()
    return asyncio.run(
        run(args.source, args.transformation, args.out, args.ref, args.old_code)
    )


if __name__ == "__main__":
    sys.exit(main())
