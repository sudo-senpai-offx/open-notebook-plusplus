#!/usr/bin/env python3
"""Replace the three learning transformations with source-grounded prompts.

Run from the repo root with the stack up:

    set -a && . ./.env && set +a
    export SURREAL_URL="ws://127.0.0.1:8108/rpc"
    .venv/bin/python scripts/rewrite-learning-prompts.py --apply

Why this is a script and not a migration
-----------------------------------------
The prompts live as `transformation` rows, so editing them means writing to the
database, and writing to the database by hand is how they ended up machine-facing
in the first place. The text belongs in version control next to the code that
consumes it, and the script is what puts it there. `--check` prints the diff
without touching anything, which is the only safe way to look at a prompt you
have not read yet.

What was wrong with the originals
---------------------------------
1. **Dense Summary was not a summary.** It asked for a Sparse Priming
   Representation and said, in its own instructions, that "the future audience
   will be another language model, not a human." This is a learning OS. A
   student is the audience.

2. **Key Insights had a topic filter.** It asked for "insights related to the
   purpose and meaning of life, human flourishing, the role of technology in the
   future of humanity, artificial intelligence and its affect on humans, memes,
   learning, reading, books, continuous improvement." That is a filter on the
   subject matter, not on the insight. Given a paper on eukaryotic transcription
   it will keep anything loosely adjacent to "learning" and discard the rest —
   which is the opposite of extracting the document's insights.

3. **Reflection Questions had the same filter**, phrased as "themes related to
   the human experience... artificial intelligence's societal impact, collective
   evolution." Questions about a chemistry paper should be about chemistry.

All three replacements are scoped to the document in front of them, and all three
are explicit about the one thing every summarisation prompt gets wrong: that
fluency is not fidelity.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import os
import sys
from typing import Dict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


DENSE_SUMMARY = """\
# ROLE

You write dense summaries of source documents for a student who has not read
them and will use your summary instead of reading the original — or will read
the original afterwards to check you.

"Dense" means the highest concentration of recoverable information per word, not
the fewest words. A dense summary is still a piece of writing a person reads top
to bottom.

# METHOD

Read the whole input before writing. Then answer, in this order:

1. **What this is** — one or two sentences: what kind of document this is, what
   it covers, and what it is trying to establish or explain.
2. **The central claims** — what the document asserts. If it argues for
   something over another thing, name both sides as the document states them.
3. **How it works** — the mechanisms, structure, method, or reasoning that the
   claims depend on. This is usually the part a summary most often drops and the
   part a reader most needs.
4. **The evidence** — what the document offers in support: data, experiments,
   cases, examples, citations. If it asserts without evidence, say so.
5. **Limits and caveats** — what the document itself limits, what it does not
   address, and where its claims are weakest *according to the document*.

Skip a section only if the document genuinely has nothing under it. A five-item
numbered summary of a paper is not dense; a 400-word account of what it claims,
how, on what evidence, and where it stops, is.

# RULES

- **Source-bound.** Use only what is in the input. Do not supply background the
  document assumes, correct it, or extend it from your own knowledge. If the
  document cites a finding without explaining it, summarise the citation, not
  the finding.
- **Attribute.** When a claim is the document's, write it as the document's. When
  something is your own inference, it does not belong in the summary at all.
- **No evaluation.** Do not judge whether the document is good, whether its
  argument is convincing, or whether you would have written it differently. A
  reader may want that; it is not a summary.
- **Prose, not notes.** Write connected paragraphs. Use a heading per section
  above. Do not produce bullet fragments — a summary made of telegraphic
  fragments is the failure mode this replaces.
- **Keep the source's technical vocabulary.** Define a term only when the
  document defines it. A summary that explains away its own field's terminology
  is less useful to an expert than one that does not.
- **Do not editorialise about the summary.** No preamble, no "Here is a summary",
  no closing note about what you could not access.

# OUTPUT

Begin immediately with the first section heading. Nothing before it."""


KEY_INSIGHTS = """\
# ROLE

You extract the insights that actually matter in the document you are given, for
a reader who wants to understand what is non-obvious about *this specific text*.

An insight is something a competent reader would consider worth their attention:
a mechanism that is not obvious from the surface, a claim that cuts against
common expectation, a consequence the document states but does not draw out, a
constraint that shapes everything else, or a distinction that the rest of the
analysis depends on.

# METHOD

1. Read the entire input first. Do not extract as you go; the value of an insight
   often depends on what appears later.
2. Identify candidate insights from the whole document.
3. Keep the ones that are genuinely load-bearing, and rank them by how much the
   reader's understanding changes if they are missing.
4. Discard candidates that are restatements of the obvious, definitions the
   reader can look up, or trivia specific to one example.

Aim for 8 to 15 insights. Return fewer if the document only supports fewer — a
thin document padded to a quota is worse than a short honest list. Never invent
an insight to reach a number.

# EACH INSIGHT MUST

- Be specific to this document. "Knowledge is important" is not an insight;
   the mechanism or claim it stands for is.
- Say *why it matters*, not just what it is. An insight without its consequence
   is a note.
- Be self-contained enough to be understood by someone who has not read the
   surrounding text.
- Distinguish what the document *asserts* from what a careful reader would
   *infer*. If you are inferring, say so. This matters more than it sounds:
   without it, your inferences get quoted later as the document's claims.

# RULES

- **Source-bound.** Use only what is in the input. Do not import outside facts,
  and do not judge the document's quality or field.
- **No topic filter.** Extract from whatever the document is about. If it is
  about transcription factors, the insights are about transcription factors. Do
  not steer toward any subject because it seems broadly interesting.
- **No padding.** Do not restate the same insight in different words to reach a
  count. Do not begin every bullet the same way.
- **No preamble, no warnings, no notes.** Output only the list.

# OUTPUT

A bulleted list, one insight per bullet, ordered most significant first. Two to
four sentences per bullet."""


REFLECTION_QUESTIONS = """\
# ROLE

You write questions a student can use to test whether they have understood a
document, and to think beyond it. The document in front of you sets the subject
matter; every question must be about it.

# METHOD

Produce questions in four tiers, in this order:

1. **Comprehension** — can the reader state the document's central claim in
   their own words? These check reading, not thinking. Two or three of them.
2. **Mechanism** — why does it work the way it does? What would have to be true
   for the claim to hold? These test whether the reader understood the argument
   rather than memorised its conclusion. Two or three.
3. **Transfer** — where else does this apply? What else does this predict that
   the document does not state? These are the ones that make the material
   usable rather than merely absorbed. Two or three.
4. **Critique** — what would change the conclusion? Where is the document
   weakest, and does it acknowledge that? What evidence would settle the
   question? These must engage a real weakness the document has, not a generic
   "what are the limitations". One or two.

Anchor every question in something specific in the document. "What is the
document's central claim?" is answerable from any text; "the document attributes
the difference between the two enzymes to what, and what evidence would
distinguish that from an alternative explanation?" is a question about *this*
document.

# RULES

- **Questions, not topics.** "Regulation of gene expression" is a topic.
  "Which transcription factor does the document identify as rate-limiting, and
  what happens to expression when it is removed?" is a question.
- **Source-bound.** Ask about what the document contains. Do not ask about
  outside facts, and do not presuppose the document's conclusions are correct.
- **Answerable from the document** for tiers 1 and 2. Tiers 3 and 4 may go past
  it, but must remain anchored to it.
- **No generic reflection.** "What does this make you think?" and "How does this
  connect to your life?" are not questions about the material. The original
  version of this prompt asked for "themes related to the human experience" and
  produced questions about AI and society for a paper about none of those.
- **No preamble, no notes.** Output only the questions.

# OUTPUT

Four sections with the tier names as headings. Under each, a bulleted list of
questions. Questions only — no answers, no explanations."""


PROMPTS: Dict[str, str] = {
    "Dense Summary": DENSE_SUMMARY,
    "Key Insights": KEY_INSIGHTS,
    "Reflection Questions": REFLECTION_QUESTIONS,
}

DESCRIPTIONS: Dict[str, str] = {
    "Dense Summary": (
        "A source-grounded summary for a reader who will use it instead of the "
        "original. Replaces a Sparse Priming Representation, which was written "
        "for a model rather than a person."
    ),
    "Key Insights": (
        "What is non-obvious and load-bearing in this specific document. "
        "Replaces a prompt with a hardcoded topic filter (life, AI, "
        "technology) that steered extraction away from the source."
    ),
    "Reflection Questions": (
        "Comprehension, mechanism, transfer and critique questions anchored in "
        "this document. Replaces a prompt that asked about human experience and "
        "AI's societal impact regardless of the subject."
    ),
}


async def main(apply: bool) -> int:
    from open_notebook.database.repository import repo_query, repo_update

    rows = await repo_query(
        "SELECT id, title, prompt, description FROM transformation WHERE title IN $titles",
        {"titles": list(PROMPTS)},
    )
    found = {str(r.get("title")): r for r in rows}

    missing = [title for title in PROMPTS if title not in found]
    if missing:
        print(f"ERROR: not in the database: {missing}", file=sys.stderr)
        print("  Nothing was changed. Create them first.", file=sys.stderr)
        return 1

    changed = 0
    for title, prompt in PROMPTS.items():
        row = found[title]
        current = str(row.get("prompt") or "")
        if (
            current == prompt
            and str(row.get("description") or "") == DESCRIPTIONS[title]
        ):
            print(f"  unchanged  {title}")
            continue
        print(f"  {'would update' if not apply else 'updating  '} {title}")
        print(
            "".join(
                difflib.unified_diff(
                    current.splitlines(keepends=True),
                    prompt.splitlines(keepends=True),
                    fromfile=f"{title} (current)",
                    tofile=f"{title} (new)",
                    n=1,
                )
            )[:400]
        )
        if apply:
            await repo_update(
                "transformation",
                str(row["id"]),
                {"prompt": prompt, "description": DESCRIPTIONS[title]},
            )
        changed += 1

    print()
    if not apply:
        print(f"{changed} would change. Re-run with --apply to write them.")
        return 0
    print(f"{changed} updated. Existing insights keep their old text —")
    print("regenerate to pick up the new prompts.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the changes; without it, only show them",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.apply)))
