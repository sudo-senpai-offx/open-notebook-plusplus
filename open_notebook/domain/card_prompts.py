"""Built-in card-generation transformation prompts (P2).

Each entry carries a machine format contract (line-based, `---`-separated
blocks) so the later pipeline (Q5-B) and `.apkg` exporter (P6) can parse
transformation output deterministically. Prompt prose encodes research-srs
§4 constraints: ONE_FACT, SHORT_A, CLOZE_FIRST, NO_LISTS.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

BUILT_IN: List[Dict[str, Any]] = [
    {
        "key": "cloze",
        "name": "flashcard_cloze",
        "title": "Flashcards (cloze)",
        "description": "Turn text into fill-in-the-blank cards, one fact each.",
        "format": "CLOZE: <sentence with {{c1::answer}}>",
        "prompt": (
            "Turn the input into cloze-deletion flashcards. Rules:\n"
            "- ONE FACT per card: one retrievable fact, split compounds.\n"
            "- Answers of 10 words or fewer: a term, date, or name.\n"
            "- Put the blank with {{c1::answer}} syntax (c2, c3 for more).\n"
            "- NO LISTS: never delete a whole clause or enumerate; split lists.\n"
            "- Blocks separated by a line with ---. Format per card:\n"
            "CLOZE: <sentence with {{c1::answer}}>\n"
            "---"
        ),
    },
    {
        "key": "basic-qa",
        "name": "flashcard_qa",
        "title": "Flashcards (Q/A)",
        "description": "Turn text into question/answer cards, one fact each.",
        "format": "Q: <question>\nA: <answer>",
        "prompt": (
            "Turn the input into question/answer flashcards. Rules:\n"
            "- ONE FACT per card: one question, one retrievable fact.\n"
            "- Answers of 10 words or fewer where possible.\n"
            "- Ask what/why/how from the text; add a WHY card before trivia.\n"
            "- NO LISTS: split enumerations into one card per item.\n"
            "- Blocks separated by a line with ---. Format per card:\n"
            "Q: <question>\n"
            "A: <answer>\n"
            "---"
        ),
    },
    {
        "key": "mcq-check",
        "name": "quiz_mcq",
        "title": "Quiz (self-check)",
        "description": "Formative multiple-choice checks; recognition only.",
        "format": "Q: <stem>\nA: <a> | B: <b> | C: <c> | D: <d>\nANSWER: <letter>",
        "prompt": (
            "Turn the input into formative multiple-choice checks. Rules:\n"
            "- ONE FACT per item; recognition only, never the retention vehicle.\n"
            "- Answers of 10 words or fewer; plausible distractors.\n"
            "- Blocks separated by a line with ---. Format per item:\n"
            "Q: <stem>\n"
            "A: <a> | B: <b> | C: <c> | D: <d>\n"
            "ANSWER: <letter>\n"
            "---"
        ),
    },
]


def get_built_in(key: str) -> Optional[Dict[str, Any]]:
    for entry in BUILT_IN:
        if entry["key"] == key:
            return entry
    return None
