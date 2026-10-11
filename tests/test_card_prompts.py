"""T5 card-prompt spot checks live here; the library is pure data."""

from open_notebook.domain.card_prompts import BUILT_IN, get_built_in


def test_three_built_ins_with_stable_keys():
    assert [b["key"] for b in BUILT_IN] == ["cloze", "basic-qa", "mcq-check"]
    for b in BUILT_IN:
        assert b["name"] and b["title"] and b["description"] and b["prompt"]


def test_prompts_encode_one_fact_and_short_answers():
    for b in BUILT_IN:
        text = b["prompt"].lower()
        assert "one fact" in text
        assert "10 words" in text


def test_prompts_declare_machine_format():
    formats = {b["key"]: b["format"] for b in BUILT_IN}
    assert formats["cloze"].startswith("CLOZE:")
    assert formats["basic-qa"].startswith("Q:")
    assert "ANSWER:" in formats["mcq-check"]
    for b in BUILT_IN:
        assert "---" in b["prompt"]


def test_get_built_in_lookup():
    assert get_built_in("cloze")["key"] == "cloze"
    assert get_built_in("nope") is None
