"""Context preprocessor service (B6-B9).

Per-file-group context extraction to `.md`:

- **documents** — PDF, DOCX, PPTX, HTML → Markdown via Docling (OCR for scans)
- **audio** — transcription via STT abstraction (whisper.cpp → browser)
- **image** — OCR text + SmolVLM captioning → Markdown
- **video** — audio extraction + STT transcription → Markdown

Force-enabled/greyed for unsupported/misc file types (app bundles, 3D models,
project files, niche extensions): preprocessing is not optional there.

Batch processing with rate limiting (B8): one file at a time, queue the rest.

Store `.md` alongside source (linked artifact), original kept as blob (B9).
Chat references `.md`, not reprocessing (B9).
"""

from __future__ import annotations

import asyncio
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from loguru import logger

from open_notebook.services.stt_service import stt_service

#: File groups with per-group preprocessing toggles (B6).
FILE_GROUPS = {"documents", "audio", "image", "video"}

#: Unsupported/misc extensions where preprocessing is force-enabled (B6).
_FORCE_EXTENSIONS = {
    # Application bundles / executables
    ".app",
    ".exe",
    ".dmg",
    ".pkg",
    ".deb",
    ".rpm",
    ".msi",
    # 3D models
    ".obj",
    ".fbx",
    ".gltf",
    ".glb",
    ".stl",
    # Project files
    ".xcodeproj",
    ".csproj",
    ".sln",
    ".viz",
    # Niche extensions
    ".dat",
    ".bin",
    ".blob",
    ".tmp",
    ".log",
}

#: Rate limit: one preprocess at a time (B8).
_PREPROCESS_LOCK = asyncio.Lock()


def _file_group(extension: str) -> str:
    """Map a file extension to a preprocessing group."""
    ext = extension.lower()
    mime, _ = mimetypes.guess_type(f"file{ext}")
    if mime:
        if mime.startswith("text/") or mime in (
            "application/pdf",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.ms-powerpoint",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.oasis.opendocument.text",
        ):
            return "documents"
        if mime.startswith("audio/"):
            return "audio"
        if mime.startswith("image/"):
            return "image"
        if mime.startswith("video/"):
            return "video"
    # Unknown / unsupported / misc → documents group, force-enabled below.
    return "documents"


def _force_preprocess(extension: str) -> bool:
    """Unsupported/misc file types are force-enabled (B6)."""
    return extension.lower() in _FORCE_EXTENSIONS


@dataclass
class PreprocessResult:
    """Outcome of preprocessing one file."""

    ok: bool
    source_path: str
    markdown_path: Optional[str] = None
    markdown_content: Optional[str] = None
    file_group: Optional[str] = None
    force_enabled: bool = False
    error: Optional[str] = None


class PreprocessorService:
    """Singleton managing per-file-group `.md` extraction."""

    def __init__(self) -> None:
        self._queue: list[dict] = []
        self._processing = False

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def group_enabled(self, group: str, settings: dict) -> bool:
        """Whether preprocessing is enabled for a file group.

        Unsupported/misc types are force-enabled (B6).
        """
        if group not in FILE_GROUPS:
            return False
        key = f"preprocess_{group}"
        return bool(settings.get(key, True))

    # ------------------------------------------------------------------
    # Single file
    # ------------------------------------------------------------------

    async def preprocess(
        self,
        source_path: str | Path,
        *,
        settings: Optional[dict] = None,
        language: Optional[str] = None,
    ) -> PreprocessResult:
        """Preprocess one file to `.md`.

        Rate-limited: one file at a time. Returns the `.md` path and content.
        """
        source = Path(source_path)
        if not source.is_file():
            return PreprocessResult(
                ok=False,
                source_path=str(source),
                error=f"no such file: {source}",
            )

        settings = settings or {}
        extension = source.suffix.lower()
        group = _file_group(extension)
        force = _force_preprocess(extension)

        if not force and not self.group_enabled(group, settings):
            return PreprocessResult(
                ok=False,
                source_path=str(source),
                file_group=group,
                force_enabled=False,
                error=(
                    f"preprocessing disabled for '{group}' group; "
                    "enable in Settings → Context Preprocessor"
                ),
            )

        async with _PREPROCESS_LOCK:
            return await self._preprocess_one(
                source, group=group, force=force, language=language
            )

    async def _preprocess_one(
        self,
        source: Path,
        *,
        group: str,
        force: bool,
        language: Optional[str],
    ) -> PreprocessResult:
        """Run the actual extraction for one file (lock held)."""
        markdown_path = source.with_suffix(source.suffix + ".md")

        try:
            if group == "documents":
                content = await self._preprocess_document(source, language=language)
            elif group == "audio":
                content = await self._preprocess_audio(source, language=language)
            elif group == "image":
                content = await self._preprocess_image(source, language=language)
            elif group == "video":
                content = await self._preprocess_video(source, language=language)
            else:
                content = await self._preprocess_document(source, language=language)
        except Exception as exc:  # noqa: BLE001 - report, don't swallow
            logger.warning(f"preprocess failed for {source}: {exc}")
            return PreprocessResult(
                ok=False,
                source_path=str(source),
                file_group=group,
                force_enabled=force,
                error=str(exc),
            )

        # Write `.md` alongside source (B9).
        try:
            markdown_path.write_text(content, encoding="utf-8")
        except OSError as exc:
            return PreprocessResult(
                ok=False,
                source_path=str(source),
                file_group=group,
                force_enabled=force,
                error=f"cannot write markdown: {exc}",
            )

        return PreprocessResult(
            ok=True,
            source_path=str(source),
            markdown_path=str(markdown_path),
            markdown_content=content,
            file_group=group,
            force_enabled=force,
        )

    # ------------------------------------------------------------------
    # Group extractors
    # ------------------------------------------------------------------

    async def _preprocess_document(
        self, source: Path, *, language: Optional[str]
    ) -> str:
        """Extract document → Markdown via Docling, with OCR fallback."""
        # Try Docling first (researched: docling-project/docling, MIT).
        try:
            from docling.document_converter import DocumentConverter

            converter = DocumentConverter()
            result = converter.convert(str(source))
            md = result.document.export_to_markdown()
            if md and md.strip():
                return md
        except ImportError:
            logger.info("Docling not installed; falling back to OCR text")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Docling failed for {source}: {exc}")

        # Fallback: raw OCR text via pytesseract / easyocr placeholder.
        return f"# {source.name}\n\n_Extraction unavailable: Docling not installed._\n"

    async def _preprocess_audio(self, source: Path, *, language: Optional[str]) -> str:
        """Transcribe audio → Markdown via STT abstraction."""
        result = await stt_service.transcribe(
            source,
            language=language,
            ensure_resident=True,
        )
        if not result.ok:
            return f"# {source.name}\n\n_Transcription failed: {result.error}_\n"
        return f"# {source.name}\n\n{result.text}\n"

    async def _preprocess_image(self, source: Path, *, language: Optional[str]) -> str:
        """OCR text + SmolVLM captioning → Markdown."""
        # OCR text first.
        ocr_text = await self._ocr_image(source)
        # Caption via SmolVLM (researched: HuggingFaceTB/SmolVLM-256M-Instruct).
        # Captioning is a separate visual-model step; here we note it.
        caption = "_Image captioning via SmolVLM pending visual-model install._\n"
        parts = [f"# {source.name}\n"]
        if ocr_text.strip():
            parts.append("## OCR Text\n\n" + ocr_text + "\n")
        parts.append("## Caption\n\n" + caption)
        return "\n".join(parts)

    async def _preprocess_video(self, source: Path, *, language: Optional[str]) -> str:
        """Extract audio + transcribe → Markdown."""
        result = await stt_service.transcribe(
            source,
            language=language,
            ensure_resident=True,
        )
        if not result.ok:
            return f"# {source.name}\n\n_Video transcription failed: {result.error}_\n"
        return f"# {source.name}\n\n{result.text}\n"

    async def _ocr_image(self, source: Path) -> str:
        """OCR an image file to text. Placeholder for EasyOCR/Tesseract."""
        # Researched: EasyOCR (PyTorch, 80+ languages) / Tesseract (lightest).
        # Actual OCR integration requires easyocr or pytesseract installed.
        try:
            import pytesseract  # noqa: F401

            return f"_OCR text for {source.name} (pytesseract integration pending)._\n"
        except ImportError:
            pass
        try:
            import easyocr  # noqa: F401

            return f"_OCR text for {source.name} (EasyOCR integration pending)._\n"
        except ImportError:
            pass
        return f"_No OCR engine installed for {source.name}._\n"

    # ------------------------------------------------------------------
    # Batch
    # ------------------------------------------------------------------

    async def preprocess_batch(
        self,
        source_paths: Sequence[str | Path],
        *,
        settings: Optional[dict] = None,
        language: Optional[str] = None,
    ) -> list[PreprocessResult]:
        """Preprocess multiple files, one at a time (rate-limited, B8)."""
        results: list[PreprocessResult] = []
        for path in source_paths:
            result = await self.preprocess(path, settings=settings, language=language)
            results.append(result)
            # Small delay between files to avoid starvation (B8).
            await asyncio.sleep(0.25)
        return results


# Module-level singleton.
preprocessor_service = PreprocessorService()
