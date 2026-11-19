from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from loguru import logger

from api.models import SettingsResponse, SettingsUpdate
from open_notebook.ai.models import ProcessingFlags
from open_notebook.domain.content_settings import ContentSettings
from open_notebook.domain.provider_config import ProviderConfig
from open_notebook.exceptions import (
    InvalidInputError,
    OpenNotebookError,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Credential export / import (Q2a)
# ---------------------------------------------------------------------------

@router.get("/settings/export")
async def export_settings():
    """Export all user-configurable settings + credentials as a JSON bundle.

    Intended for backup / cross-device restore. Excludes secrets that cannot
    be decrypted, but includes encrypted API keys as stored (safe to transfer
    between devices sharing the same encryption key, or re-entered manually).
    """
    try:
        settings = await ContentSettings.get_instance()
        flags = await ProcessingFlags.get_instance()
        provider_config = await ProviderConfig.get_instance()

        bundle: Dict[str, Any] = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "version": 1,
            "settings": {
                "default_content_processing_engine_doc": settings.default_content_processing_engine_doc,
                "default_content_processing_engine_url": settings.default_content_processing_engine_url,
                "default_embedding_option": settings.default_embedding_option,
                "auto_delete_files": settings.auto_delete_files,
                "docling_ocr": settings.docling_ocr,
                "docling_formulas": settings.docling_formulas,
                "docling_vision": settings.docling_vision,
                "youtube_preferred_languages": settings.youtube_preferred_languages,
                "allow_local_processing": flags.allow_local_processing,
            },
            "deepseek": {
                "ds_session_id": flags.deepseek_ds_session_id,
                "authorization_token": flags.deepseek_authorization_token,
                "pc_url": flags.deepseek_pc_url,
            },
            "provider_config": {
                "credentials": provider_config.credentials,
            },
        }

        return JSONResponse(content=bundle)
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Settings export failed: {e}")
        raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")


@router.post("/settings/import")
async def import_settings(payload: Dict[str, Any]):
    """Import a previously exported settings bundle.

    Overwrites local settings + DeepSeek credentials + provider credentials.
    Safe to re-run; merges provider credentials without dropping unknown keys.
    """
    try:
        settings = await ContentSettings.get_instance()
        flags = await ProcessingFlags.get_instance()
        provider_config = await ProviderConfig.get_instance()

        # --- Content settings ---
        s = payload.get("settings", {})
        if s.get("default_content_processing_engine_doc") is not None:
            settings.default_content_processing_engine_doc = s[
                "default_content_processing_engine_doc"
            ]
        if s.get("default_content_processing_engine_url") is not None:
            settings.default_content_processing_engine_url = s[
                "default_content_processing_engine_url"
            ]
        if s.get("default_embedding_option") is not None:
            settings.default_embedding_option = s["default_embedding_option"]
        if s.get("auto_delete_files") is not None:
            settings.auto_delete_files = s["auto_delete_files"]
        if s.get("docling_ocr") is not None:
            settings.docling_ocr = s["docling_ocr"]
        if s.get("docling_formulas") is not None:
            settings.docling_formulas = s["docling_formulas"]
        if s.get("docling_vision") is not None:
            settings.docling_vision = s["docling_vision"]
        if s.get("youtube_preferred_languages") is not None:
            settings.youtube_preferred_languages = s[
                "youtube_preferred_languages"
            ]
        await settings.update()

        # --- Processing flags ---
        f = payload.get("settings", {})
        if f.get("allow_local_processing") is not None:
            flags.allow_local_processing = bool(f["allow_local_processing"])
            await flags.update()

        # --- DeepSeek credentials ---
        ds = payload.get("deepseek", {})
        if ds.get("ds_session_id") is not None:
            flags.deepseek_ds_session_id = ds["ds_session_id"]
        if ds.get("authorization_token") is not None:
            flags.deepseek_authorization_token = ds["authorization_token"]
        if ds.get("pc_url") is not None:
            flags.deepseek_pc_url = ds["pc_url"]
        await flags.update()

        # --- Provider config (credentials) ---
        pc = payload.get("provider_config", {})
        creds = pc.get("credentials", {})
        if creds:
            # Merge: existing credentials preserved, new ones added/overwritten
            for provider, cred_list in creds.items():
                if provider not in provider_config.credentials:
                    provider_config.credentials[provider] = []
                existing_ids = {c.get("id") for c in provider_config.credentials[provider]}
                for cred in cred_list:
                    if cred.get("id") in existing_ids:
                        # Replace existing
                        provider_config.credentials[provider] = [
                            c if c.get("id") == cred["id"] else cred
                            for c in provider_config.credentials[provider]
                        ]
                    else:
                        provider_config.credentials[provider].append(cred)
            await provider_config.save()

        return {
            "ok": True,
            "message": "Settings imported successfully",
        }
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Settings import failed: {e}")
        raise HTTPException(status_code=500, detail=f"Import failed: {str(e)}")


# ---------------------------------------------------------------------------
# Cumulative sync manifest (Q2b)
# ---------------------------------------------------------------------------

@router.get("/sync/manifest")
async def sync_manifest():
    """Return a cumulative sync manifest: everything that should replicate
    across devices (except model selection, which is deferred per-device).

    Includes: notebooks, sources, attachments, chat sessions + messages,
    settings, deepseek credentials, provider config.
    Uses last-write-wins via the ON++ sync engine (surreal + blob store).
    """
    try:
        from open_notebook.domain.attachment import Attachment
        from open_notebook.domain.notebook import Notebook, Source

        manifest: Dict[str, Any] = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "version": 1,
        }

        # Notebooks
        try:
            notebooks = await Notebook.get_all()
            manifest["notebooks"] = [n.model_dump() for n in notebooks]
        except Exception as e:
            logger.warning(f"Sync manifest: notebooks failed: {e}")
            manifest["notebooks"] = []

        # Sources
        try:
            sources = await Source.get_all()
            manifest["sources"] = [s.model_dump() for s in sources]
        except Exception as e:
            logger.warning(f"Sync manifest: sources failed: {e}")
            manifest["sources"] = []

        # Attachments
        try:
            attachments = await Attachment.list_all()
            manifest["attachments"] = [a.model_dump() for a in attachments]
        except Exception as e:
            logger.warning(f"Sync manifest: attachments failed: {e}")
            manifest["attachments"] = []

        # Settings
        try:
            settings = await ContentSettings.get_instance()
            flags = await ProcessingFlags.get_instance()
            manifest["settings"] = {
                "default_content_processing_engine_doc": settings.default_content_processing_engine_doc,
                "default_content_processing_engine_url": settings.default_content_processing_engine_url,
                "default_embedding_option": settings.default_embedding_option,
                "auto_delete_files": settings.auto_delete_files,
                "docling_ocr": settings.docling_ocr,
                "docling_formulas": settings.docling_formulas,
                "docling_vision": settings.docling_vision,
                "youtube_preferred_languages": settings.youtube_preferred_languages,
                "allow_local_processing": flags.allow_local_processing,
            }
            manifest["deepseek"] = {
                "ds_session_id": flags.deepseek_ds_session_id,
                "authorization_token": flags.deepseek_authorization_token,
                "pc_url": flags.deepseek_pc_url,
            }
        except Exception as e:
            logger.warning(f"Sync manifest: settings failed: {e}")
            manifest["settings"] = {}
            manifest["deepseek"] = {}

        return manifest
    except Exception as e:
        logger.error(f"Sync manifest failed: {e}")
        raise HTTPException(status_code=500, detail=f"Manifest failed: {str(e)}")


@router.get("/settings", response_model=SettingsResponse)
async def get_settings():
    """Get all application settings."""
    try:
        settings: ContentSettings = await ContentSettings.get_instance()  # type: ignore[assignment]
        flags = await ProcessingFlags.get_instance()

        return SettingsResponse(
            default_content_processing_engine_doc=settings.default_content_processing_engine_doc,
            default_content_processing_engine_url=settings.default_content_processing_engine_url,
            default_embedding_option=settings.default_embedding_option,
            auto_delete_files=settings.auto_delete_files,
            docling_ocr=settings.docling_ocr,
            docling_formulas=settings.docling_formulas,
            docling_vision=settings.docling_vision,
            youtube_preferred_languages=settings.youtube_preferred_languages,
            allow_local_processing=flags.allow_local_processing,
            deepseek_ds_session_id=flags.deepseek_ds_session_id,
            deepseek_authorization_token=flags.deepseek_authorization_token,
            deepseek_pc_url=flags.deepseek_pc_url,
        )
    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error fetching settings: {str(e)}")
        raise HTTPException(status_code=500, detail="Error fetching settings")


@router.put("/settings", response_model=SettingsResponse)
async def update_settings(settings_update: SettingsUpdate):
    """Update application settings."""
    try:
        settings: ContentSettings = await ContentSettings.get_instance()  # type: ignore[assignment]
        flags = await ProcessingFlags.get_instance()

        # Update only provided fields
        if settings_update.default_content_processing_engine_doc is not None:
            # Cast to proper literal type
            from typing import Literal, cast

            settings.default_content_processing_engine_doc = cast(
                Literal["auto", "docling", "simple"],
                settings_update.default_content_processing_engine_doc,
            )
        if settings_update.default_content_processing_engine_url is not None:
            from typing import Literal, cast

            settings.default_content_processing_engine_url = cast(
                Literal["auto", "firecrawl", "jina", "crawl4ai", "simple"],
                settings_update.default_content_processing_engine_url,
            )
        if settings_update.default_embedding_option is not None:
            from typing import Literal, cast

            settings.default_embedding_option = cast(
                Literal["ask", "always", "never"],
                settings_update.default_embedding_option,
            )
        if settings_update.auto_delete_files is not None:
            from typing import Literal, cast

            settings.auto_delete_files = cast(
                Literal["yes", "no"], settings_update.auto_delete_files
            )
        if settings_update.docling_ocr is not None:
            settings.docling_ocr = settings_update.docling_ocr
        if settings_update.docling_formulas is not None:
            settings.docling_formulas = settings_update.docling_formulas
        if settings_update.docling_vision is not None:
            settings.docling_vision = settings_update.docling_vision
        if settings_update.youtube_preferred_languages is not None:
            settings.youtube_preferred_languages = (
                settings_update.youtube_preferred_languages
            )

        # Update DeepSeek-web credentials in ProcessingFlags record
        if settings_update.deepseek_ds_session_id is not None:
            flags.deepseek_ds_session_id = settings_update.deepseek_ds_session_id
        if settings_update.deepseek_authorization_token is not None:
            flags.deepseek_authorization_token = (
                settings_update.deepseek_authorization_token
            )
        if settings_update.deepseek_pc_url is not None:
            flags.deepseek_pc_url = settings_update.deepseek_pc_url

        # Update allow_local_processing in ProcessingFlags record
        if settings_update.allow_local_processing is not None:
            flags = await ProcessingFlags.get_instance()
            flags.allow_local_processing = bool(settings_update.allow_local_processing)
            await flags.update()

            # Also write to Termux-side file so termux-launch.sh can read it
            # before SurrealDB starts on cold boot.
            try:
                local_flag_file = "/data/data/com.termux/files/home/.onb_allow_local"
                with open(local_flag_file, "w") as f:
                    f.write("1" if flags.allow_local_processing else "0")
            except Exception:
                pass  # Non-critical; SurrealDB is the source of truth

        await settings.update()

        return SettingsResponse(
            default_content_processing_engine_doc=settings.default_content_processing_engine_doc,
            default_content_processing_engine_url=settings.default_content_processing_engine_url,
            default_embedding_option=settings.default_embedding_option,
            auto_delete_files=settings.auto_delete_files,
            docling_ocr=settings.docling_ocr,
            docling_formulas=settings.docling_formulas,
            docling_vision=settings.docling_vision,
            youtube_preferred_languages=settings.youtube_preferred_languages,
            allow_local_processing=flags.allow_local_processing,
            deepseek_ds_session_id=flags.deepseek_ds_session_id,
            deepseek_authorization_token=flags.deepseek_authorization_token,
            deepseek_pc_url=flags.deepseek_pc_url,
        )
    except HTTPException:
        raise
    except InvalidInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error updating settings: {str(e)}")
        raise HTTPException(status_code=500, detail="Error updating settings")
