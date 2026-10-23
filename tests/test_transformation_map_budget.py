"""The map step's per-part budget, and why it is the size it is.

A part of a long document is an intermediate, not the answer. It has to
*finish*: a part that is interrupted is a part whose work is thrown away, and
the parts are what the resume is made of - so the budget is set by how long a
part takes on the hardware this runs on, not by how much text would be nice to
have.

The default was five hundred tokens, and every device without the setting -
which is every device except the one it was decided on - inherited it. On the
floor this project supports that is a part longer than the interruptions the
device hands out, so no part ever completed to be kept.
"""

from __future__ import annotations

from unittest.mock import patch

from open_notebook.graphs.transformation import (
    DEFAULT_MAP_OUTPUT_TOKENS,
    _map_output_tokens,
)

SETTING = "OPEN_NOTEBOOK_TRANSFORMATION_MAP_TOKENS"


def test_the_default_is_the_size_that_finishes():
    """A part has to be able to finish.

    Measured on the floor this supports - a phone, qwen2.5-1.5b at two
    threads - 250 tokens of generation is about 152 s. Five hundred is twice
    that, on top of the prompt evaluation every part pays anyway: longer than
    such a device lets a background job run, so the work was thrown away.
    """
    assert DEFAULT_MAP_OUTPUT_TOKENS == 250


def test_the_setting_overrides_the_default():
    """The configuration is a wish, and it is honoured when it can be."""
    with patch.dict("os.environ", {SETTING: "400"}):
        assert _map_output_tokens() == 400


def test_an_unusable_value_falls_back_to_the_default():
    """A setting nobody can parse must not become something else quietly -
    least of all the size that was measured to be wrong."""
    for unusable in ("", "   ", "many", "0", "-5", "250.5"):
        with patch.dict("os.environ", {SETTING: unusable}):
            assert _map_output_tokens() == DEFAULT_MAP_OUTPUT_TOKENS, unusable
