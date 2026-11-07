"""Prompt library service (G24/G25).

Versioned prompts per feature, stored as JSON in prompts/library.json.
Prompts are research-validated (no hallucination) and support optional
custom prompts appended per feature (G25 Q13).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from loguru import logger

PROMPTS_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "library.json"


@dataclass
class Prompt:
    """A versioned prompt with variables."""

    name: str
    description: str
    variables: list[str]
    template: str


class PromptLibrary:
    """Loads and renders versioned prompts from the library JSON."""

    def __init__(self, path: Optional[Path] = None):
        self._path = path or PROMPTS_PATH
        self._prompts: dict[str, Prompt] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.warning(f"Prompt library not loadable: {exc}")
            return
        for name, spec in data.get("prompts", {}).items():
            self._prompts[name] = Prompt(
                name=name,
                description=spec.get("description", ""),
                variables=spec.get("variables", []),
                template=spec.get("template", ""),
            )

    def list(self) -> list[str]:
        """Return all prompt names."""
        return sorted(self._prompts.keys())

    def get(self, name: str) -> Optional[Prompt]:
        """Return a prompt by name, or None."""
        return self._prompts.get(name)

    def render(self, name: str, **variables) -> Optional[str]:
        """Render a prompt template with variables.

        Missing variables are replaced with empty strings; extra variables
        are ignored. Returns None if the prompt doesn't exist.
        """
        prompt = self.get(name)
        if prompt is None:
            return None
        safe = {var: variables.get(var, "") for var in prompt.variables}
        try:
            return prompt.template.format(**safe)
        except (KeyError, ValueError) as exc:
            logger.warning(f"Prompt render failed for {name}: {exc}")
            return None


# Module-level singleton.
prompt_library = PromptLibrary()
