"""Prompter service — session-aware prompt transformation for DeepSeek-web.

Takes raw user input + session context + notebook context and transforms it
into a highly refined, purpose-oriented prompt optimized for DeepSeek-web web
interface. Uses session history, notebook context, and processing rules to
produce prompts that get consistent, accurate responses without requiring the
user to be a prompt engineer.
"""

from __future__ import annotations

from typing import List

from loguru import logger

from open_notebook.domain.notebook import ChatSession, Notebook


class PrompterService:
    """Transforms user messages into DeepSeek-web-optimized prompts using session context."""

    def __init__(self) -> None:
        self._default_prefix = (
            "You are a helpful AI assistant accessed through Open Notebook Plus Plus. "
            "Please provide accurate, well-reasoned, and comprehensive answers. "
            "When appropriate, cite sources and explain your reasoning step by step."
        )
        self._default_suffix = (
            "\n\nPlease respond in a clear, structured format. If you use external "
            "information, please indicate its source. If you are unsure about something, "
            "say so rather than hallucinating."
        )

    async def transform(
        self,
        raw_message: str,
        session_id: str | None = None,
        notebook_id: str | None = None,
        message_type: str = "chat",  # chat, summarize, analyze, creative, code
        system_instructions: str | None = None,
    ) -> str:
        """Transform a raw user message into a DeepSeek-web-optimized prompt.

        Args:
            raw_message: The user's raw input message.
            session_id: Current chat session ID (for context).
            notebook_id: Current notebook ID (for notebook context).
            message_type: Type of processing — chat, summarize, analyze, creative, code.
            system_instructions: Optional system-level instructions to prepend.

        Returns:
            A refined prompt string ready for DeepSeek-web.
        """
        parts: List[str] = []

        # 1. System instructions (highest priority)
        if system_instructions:
            parts.append(f"System Instructions: {system_instructions}")
        else:
            parts.append(self._default_prefix)

        # 2. Session context (previous messages if available)
        session_context = await self._get_session_context(session_id)
        if session_context:
            parts.append(f"\nSession Context:\n{session_context}")

        # 3. Notebook context (sources, notes if available)
        notebook_context = await self._get_notebook_context(notebook_id)
        if notebook_context:
            parts.append(f"\nNotebook Context:\n{notebook_context}")

        # 4. Message-type-specific framing
        type_instruction = self._get_type_instruction(message_type)
        if type_instruction:
            parts.append(f"\nTask Instructions: {type_instruction}")

        # 5. The actual user message
        parts.append(f"\nUser Message: {raw_message}")

        # 6. Output format instruction
        parts.append(self._default_suffix)

        prompt = "\n".join(parts)
        logger.debug(f"Prompter transformed prompt ({len(prompt)} chars) for session {session_id}")
        return prompt

    async def _get_session_context(self, session_id: str | None) -> str | None:
        """Fetch recent conversation context from a session."""
        if not session_id:
            return None
        try:
            session = await ChatSession.get(session_id)
            if session:
                return f"Session title: {session.title or 'Untitled'}"
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Could not load session context for {session_id}: {exc}")
        return None

    async def _get_notebook_context(self, notebook_id: str | None) -> str | None:
        """Fetch notebook context (title, description, source count)."""
        if not notebook_id:
            return None
        try:
            notebook = await Notebook.get(notebook_id)
            if notebook:
                return (
                    f"Notebook: {notebook.name}\n"
                    f"Description: {notebook.description or 'No description'}"
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Could not load notebook context for {notebook_id}: {exc}")
        return None

    def _get_type_instruction(self, message_type: str) -> str | None:
        """Return type-specific instruction text."""
        instructions = {
            "chat": "Respond naturally and conversationally. Address the user's question directly.",
            "summarize": "Provide a concise summary of the key points. Use bullet points for clarity.",
            "analyze": "Perform a detailed analysis. Break down the components, identify patterns, and provide insights.",
            "creative": "Be creative and imaginative. Use vivid language and explore multiple perspectives.",
            "code": "Write clean, well-documented code. Explain the logic and any assumptions made.",
        }
        return instructions.get(message_type)

    def update_prefix(self, prefix: str) -> None:
        """Update the default system prefix."""
        self._default_prefix = prefix

    def update_suffix(self, suffix: str) -> None:
        """Update the default output suffix."""
        self._default_suffix = suffix


# Singleton instance
prompter_service = PrompterService()
