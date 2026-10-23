import os
from typing import MutableMapping

# ROOT DATA FOLDER
DATA_FOLDER = "./data"

# LANGGRAPH CHECKPOINT FILE
sqlite_folder = f"{DATA_FOLDER}/sqlite-db"
os.makedirs(sqlite_folder, exist_ok=True)
LANGGRAPH_CHECKPOINT_FILE = f"{sqlite_folder}/checkpoints.sqlite"

# UPLOADS FOLDER
UPLOADS_FOLDER = f"{DATA_FOLDER}/uploads"
os.makedirs(UPLOADS_FOLDER, exist_ok=True)

# BLOBS FOLDER - content-addressed store for source files.
#
# Replaces UPLOADS_FOLDER for new ingestions. Files are named by the
# sha256 of their contents, so a file added twice occupies one path and
# the hash is the identity that survives crossing to another device.
# UPLOADS_FOLDER stays defined because sources already on disk live
# there; nothing moves until they are re-ingested.
BLOBS_FOLDER = f"{DATA_FOLDER}/blobs"
os.makedirs(BLOBS_FOLDER, exist_ok=True)

# PODCASTS FOLDER
# Matches the root that build_episode_output_dir() (commands/podcast_commands.py)
# creates episode directories under when called with DATA_FOLDER in production.
PODCASTS_FOLDER = f"{DATA_FOLDER}/podcasts"
os.makedirs(PODCASTS_FOLDER, exist_ok=True)
# Podcast generation presets (opt-in, disabled by default)
OPEN_NOTEBOOK_PODCAST_FAST_PRESET = os.getenv(
    "OPEN_NOTEBOOK_PODCAST_FAST_PRESET", "false"
).lower() in (
    "true",
    "1",
    "yes",
    "on",
)
OPEN_NOTEBOOK_PODCAST_DRAFT_PRESET = os.getenv(
    "OPEN_NOTEBOOK_PODCAST_DRAFT_PRESET", "false"
).lower() in (
    "true",
    "1",
    "yes",
    "on",
)
OPEN_NOTEBOOK_PODCAST_FAST_MAX_SEGMENTS = int(
    os.getenv("OPEN_NOTEBOOK_PODCAST_FAST_MAX_SEGMENTS", "3")
)
OPEN_NOTEBOOK_PODCAST_FAST_MAX_TOKENS = int(
    os.getenv("OPEN_NOTEBOOK_PODCAST_FAST_MAX_TOKENS", "500")
)


# TIKTOKEN CACHE FOLDER
# Reads TIKTOKEN_CACHE_DIR from the environment so Docker can redirect the cache
# to a path outside /data/ (which is typically volume-mounted and would hide the
# pre-baked encoding baked into the image at build time).
TIKTOKEN_CACHE_DIR = (
    os.environ.get("TIKTOKEN_CACHE_DIR", "").strip() or f"{DATA_FOLDER}/tiktoken-cache"
)
os.makedirs(TIKTOKEN_CACHE_DIR, exist_ok=True)

# LLM TIMEOUT
# Since esperanto 2.28, to_langchain() enforces ESPERANTO_LLM_TIMEOUT on every
# provider (default 60 s; before, most providers used their SDK default and
# Ollama waited forever). 60 s cuts off long answers, so Open Notebook used to
# default it to 180 s - which is right for a cloud API answering a chat reply,
# and three times too short for the hardware this is meant to run on.
#
# Measured on the floor this supports: a phone, qwen2.5-1.5b at two threads,
# one 2,221-token part of a book. Evaluating the prompt took 264 s; writing
# 250 tokens took 152 s. One part of one document, 417 s, against a 180 s
# budget - so every part of every long document failed, and the failure said
# the provider could not be reached. On the floor's own floor - a slower phone
# still - the reduce step alone is twice that.
#
# A generous timeout does not slow down the failure that matters. A server
# that is not there refuses the connection at once, and the classifier says
# so; the timeout bounds a request that was accepted and has not answered,
# which on this hardware is the normal case rather than the exceptional one.
# An explicit value always wins. Both the API and the worker import this
# module before any model is created.
DEFAULT_LLM_TIMEOUT_SECONDS = 1800


def ensure_llm_timeout_default(environ: MutableMapping[str, str] = os.environ) -> None:
    """Set ESPERANTO_LLM_TIMEOUT to Open Notebook's default when unset or blank."""
    if not environ.get("ESPERANTO_LLM_TIMEOUT", "").strip():
        environ["ESPERANTO_LLM_TIMEOUT"] = str(DEFAULT_LLM_TIMEOUT_SECONDS)


ensure_llm_timeout_default()
