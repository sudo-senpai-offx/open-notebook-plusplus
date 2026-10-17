#!/usr/bin/env python3
"""Check whether a GGUF embedding model will load in llama.cpp.

Why this exists
---------------
`llama-server` refuses a BERT-architecture GGUF whose metadata omits the
token-type count:

    llama_model_load: error loading model: bert model needs to define token type count

That cost two failed phone deployments before it was understood. The failing
model was a community conversion of `multilingual-e5-small`; the metadata key
is `tokenizer.ggml.token_type_count` (llama.cpp's `LLM_KV_TOKENIZER_TOKEN_TYPE_COUNT`),
and that conversion simply does not write it.

Reading the key needs only the first few megabytes of the file, so this can be
checked over HTTP with a range request - before downloading 400 MB to a
memory-constrained phone - and before believing a README that says the model
works.

The check is only as good as its controls, so `--self-test` verifies it against
two known files: one that loads and one that does not.

Usage:
    scripts/check-gguf.py <url> [<url> ...]
    scripts/check-gguf.py --self-test
"""

from __future__ import annotations

import argparse
import struct
import sys
import urllib.request

#: Metadata key llama.cpp reads for BERT models. Found in
#: src/llama-arch.cpp as LLM_KV_TOKENIZER_TOKEN_TYPE_COUNT. It is *not*
#: `bert.token_type_count`, which is the name that reads naturally and which
#: an earlier version of this script searched for - finding nothing in files
#: that load perfectly well.
TOKEN_TYPE_KEY = b"tokenizer.ggml.token_type_count"

#: The metadata block sits at the head of the file, but the tokenizer vocab
#: arrays run to several megabytes, so the window has to be generous. Files
#: smaller than this are read whole.
WINDOW = 24 * 1024 * 1024

GGUF_MAGIC = b"GGUF"


def _fetch_head(url: str, window: int = WINDOW) -> bytes:
    """The first *window* bytes of *url*, following redirects.

    HuggingFace answers a range request with 206 and the partial body. Servers
    that ignore Range answer 200 with the whole file; taking the head of that
    is still correct.
    """
    request = urllib.request.Request(url, headers={"Range": f"bytes=0-{window - 1}"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read(window)


def _find(haystack: bytes, needle: bytes) -> int:
    return haystack.find(needle)


def inspect(url: str, window: int = WINDOW) -> tuple[bool, str]:
    """Whether *url* looks loadable as a BERT embedding model.

    Returns ``(ok, detail)``. A missing key is reported as a refusal to load,
    because that is what llama.cpp does with it - and a model that cannot load
    is worse than one that is slow.
    """
    try:
        head = _fetch_head(url, window)
    except Exception as exc:  # noqa: BLE001 - any transport failure is a "cannot check"
        return False, f"could not fetch: {exc}"

    if head[:4] != GGUF_MAGIC:
        return False, f"not a GGUF file (magic {head[:4]!r})"

    at = _find(head, TOKEN_TYPE_KEY)
    if at < 0:
        return False, (
            f"{TOKEN_TYPE_KEY.decode()} absent - llama.cpp will reject this "
            f"with 'bert model needs to define token type count'"
        )

    # After the key: a uint32 value type, then the value. Only scalar types
    # are meaningful here; anything else is reported rather than guessed.
    offset = at + len(TOKEN_TYPE_KEY)
    if offset + 8 > len(head):
        return False, "key found but value is past the fetched window"
    value_type = struct.unpack_from("<I", head, offset)[0]
    if value_type != 4:  # GGUF_TYPE_UINT32
        return False, f"unexpected value type {value_type} for the token-type count"
    count = struct.unpack_from("<I", head, offset + 4)[0]
    if count == 0:
        return False, "token_type_count is 0, which is what llama.cpp rejects"
    return True, f"{TOKEN_TYPE_KEY.decode()} = {count} - should load"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("urls", nargs="*", help="GGUF URLs to check")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="verify the check against one loadable and one unloadable file",
    )
    args = parser.parse_args(argv)

    if args.self_test:
        cases = [
            (
                "should LOAD",
                "https://huggingface.co/gpustack/bge-m3-GGUF/resolve/main/bge-m3-Q4_K_M.gguf",
                True,
            ),
            (
                "should REJECT",
                "https://huggingface.co/cstr/multilingual-e5-small-GGUF/resolve/main/"
                "multilingual-e5-small-q8_0.gguf",
                False,
            ),
        ]
        failures = 0
        for label, url, expected in cases:
            ok, detail = inspect(url)
            verdict = "PASS" if ok is expected else "FAIL"
            if ok is not expected:
                failures += 1
            print(f"  [{verdict}] {label}: {detail}")
        if failures:
            print(f"self-test failed: {failures} case(s) disagreed with reality")
        return 1 if failures else 0

    if not args.urls:
        parser.error("give at least one URL, or --self-test")

    failures = 0
    for url in args.urls:
        ok, detail = inspect(url)
        if not ok:
            failures += 1
        name = url.rsplit("/", 1)[-1]
        print(f"  {'LOADS' if ok else 'REJECTED':<9} {name}\n            {detail}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
