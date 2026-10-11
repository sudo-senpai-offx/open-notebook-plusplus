"""Surreal-commands integration for Open Notebook"""

# The worker starts via `surreal-commands-worker --import-modules commands`,
# so this package is imported before the worker connects to SurrealDB. Inject
# the internal DB hosts into no_proxy first so the DB websocket is never
# tunnelled through a configured HTTP proxy (issue #1160).
from open_notebook.utils.proxy import ensure_internal_no_proxy
from open_notebook.utils.retry_visibility import install_retry_visibility

ensure_internal_no_proxy()
try:
    from open_notebook.utils.resource_tuning import apply_tuning

    apply_tuning()
except Exception:
    pass

# Surface deep command retries (B2). The worker runs at INFO and
# `process_source` logs every one of its 15 attempts at DEBUG, so a command
# stuck deep in the queue is invisible; this adds a milestone-only handler.
# Installed here because the worker imports this package after
# `configure_logging`, so the handler survives that call.
install_retry_visibility()

from .embedding_commands import (
    embed_insight_command,
    embed_note_command,
    embed_source_command,
    rebuild_embeddings_command,
)
from .podcast_commands import generate_podcast_command
from .site_commands import crawl_site_command
from .source_commands import process_source_command
from .training_commands import train_tiny_command

__all__ = [
    # Embedding commands
    "embed_note_command",
    "embed_insight_command",
    "embed_source_command",
    "rebuild_embeddings_command",
    # Other commands
    "generate_podcast_command",
    "process_source_command",
    "crawl_site_command",
    "train_tiny_command",
]
