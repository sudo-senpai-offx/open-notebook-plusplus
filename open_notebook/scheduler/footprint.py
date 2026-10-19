"""What a set of in-flight jobs costs in resident memory.

The correction this exists to encode
------------------------------------
The first cut of the admission design charged each job its own model footprint.
That is wrong, and wrong in the expensive direction: three transformations
running against the same model need **one** resident model, not three. Charging
per job would have held work that was perfectly safe, which is the mirror of
the original bug (charging nothing, and admitting three 6 GB jobs onto a 15 GB
box) and just as wrong.

So the unit of cost is the **model**, and the question the gate asks is "which
distinct models would be resident if I admitted this job?" Not "how many jobs".

Where the numbers come from
---------------------------
Model weights are derived from bytes on disk, reported by Ollama's model list,
multiplied by a factor for runtime overhead. Embedding and other small models
are the exception: they are few enough to state directly.

What is deliberately *not* here
-------------------------------
KV cache. It scales with context length and parallel requests, not with weight
bytes, and neither is known before a job runs. Folding it into a constant
multiplier produces a number that looks derived and is actually guessed. It is
absorbed by the reserve instead, which is the honest place for an unknown of
this shape - a reserve that is sized for the largest plausible context, rather
than a footprint that pretends to know the context.

A design review flagged the constant multiplier for exactly this. The constant
is kept, but it is documented as an overhead estimate rather than a model of
the runtime, and it is not the only defence: the ceiling check is independent
of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable

#: Runtime overhead on top of weight bytes. Covers the allocator's working
#: arena and activations, not the KV cache. Measured indirectly: a Q4_K_M 4B
#: model at 5.09 GB on disk settles near 5.6 GB resident with a short context.
WEIGHT_OVERHEAD = 1.10

#: Resident cost of models small enough to state rather than derive. Anything
#: in the hundreds of MB is dominated by overhead and context, not weights.
#:
#: Two kinds of key live here deliberately. A model name ("bge-m3") is used
#: when the caller knows which model it is sizing. A category key
#: ("embedding", "generation") is used by `job_footprint`, which knows only
#: what kind of work a command does and not which model will serve it. Both
#: must be present: an earlier version had only model names, and every
#: embedding job resolved to zero cost and reported itself uncertain.
EXPLICIT_FOOTPRINTS: Dict[str, int] = {
    # category keys
    "embedding": 1_200 * 1024 * 1024,
    # Speech. Stated rather than derived because the *weights* are a small part
    # of the cost: whisper base.en q5_1 is 57 MB of weights but its decode
    # scratch on aarch64 runs to a couple of hundred MB, and piper's onnxruntime
    # arena is larger than the voice. Charging the weights would understate both
    # by a factor of three.
    #
    # Deliberately a floor, not a ceiling. Overstating holds a job that might
    # have run; understating is an OOM kill of the model server, which takes the
    # embedding and chat models down with it.
    "transcription": 256 * 1024 * 1024,
    "synthesis": 256 * 1024 * 1024,
    "__no_model__": 64 * 1024 * 1024,
    # model names, for callers that know the specific model
    "bge-m3": 1_200 * 1024 * 1024,
    "bge-small": 400 * 1024 * 1024,
    "nomic-embed-text": 300 * 1024 * 1024,
}


@dataclass(frozen=True)
class ModelRequirement:
    """One model a set of jobs would need resident.

    `key` is what identifies the model for dedup purposes - two jobs naming the
    same key share one resident model. `resident` is the cost if it has to be
    loaded because of this job alone.
    """

    key: str
    resident: int


@dataclass
class FootprintEstimate:
    """The aggregate cost of a candidate set of jobs."""

    #: Distinct models, keyed. This is the number that matters: the sum of
    #: these is what would be resident, because shared models load once.
    models: Dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.models.values())

    def merge(self, other: "FootprintEstimate") -> "FootprintEstimate":
        merged = dict(self.models)
        for key, cost in other.models.items():
            # A model shared by both sets is paid for once. max(), not sum() -
            # summing would charge two resident copies of one model.
            merged[key] = max(merged.get(key, 0), cost)
        return FootprintEstimate(models=merged)

    def key_set(self) -> frozenset:
        return frozenset(self.models)


def model_footprint(model_key: str, disk_bytes: int | None = None) -> int:
    """Resident cost for one model.

    Prefers an explicit entry; otherwise derives from `disk_bytes`. Returns 0
    for an unknown model with no size, which callers must treat as *unknown*
    rather than free - see `FootprintEstimate.uncertain`.
    """
    if model_key in EXPLICIT_FOOTPRINTS:
        return EXPLICIT_FOOTPRINTS[model_key]
    if disk_bytes and disk_bytes > 0:
        return int(disk_bytes * WEIGHT_OVERHEAD)
    return 0


@dataclass
class JobFootprint:
    """What one job would add to the resident set."""

    models: Dict[str, int] = field(default_factory=dict)
    #: True when a model could not be sized. The gate holds rather than
    #: promotes, because "I could not find out how big this is" is not the
    #: same as "this is small".
    uncertain: bool = False

    @property
    def total(self) -> int:
        return sum(self.models.values())


#: Which command kinds need which model. Keyed by the `name` on a command row.
#:
#: The default is `__no_model__` and it is deliberately the default rather than
#: empty: a command kind nobody has classified should be assumed to be cheap
#: *or* the table is wrong. It is cheap-assumed here because most commands are
#: plumbing, and the generation and embedding kinds below are the ones that
#: actually move RAM.
COMMAND_MODEL: Dict[str, str] = {
    "process_source": "__no_model__",
    "crawl_site": "__no_model__",
    "ingest_vault": "__no_model__",
    "create_insight": "__no_model__",
    "rebuild_embeddings": "__no_model__",
    "prune_event_logs": "__no_model__",
    # Generation. The model is supplied per job in args and resolved by the
    # caller, which is why these are looked up dynamically below.
    "run_transformation": "__generation__",
    "generate_podcast": "__generation__",
    # Embedding.
    "embed_source": "__embedding__",
    "embed_insight": "__embedding__",
    "embed_note": "__embedding__",
    # Speech. Sized as *known* models rather than derived from disk: both servers
    # live outside the guest, on the phone's Termux side, so the bytes-on-disk
    # figure that generation and embedding are sized from is not visible from
    # here. The category key is the honest description of what is known.
    "synthesize_speech": "synthesis",
}


def job_footprint(
    command_name: str,
    *,
    generation_bytes: int | None = None,
    embedding_bytes: int | None = None,
) -> JobFootprint:
    """What one job of `command_name` would add.

    `generation_bytes` / `embedding_bytes` are the on-disk sizes Ollama reports,
    passed in so this function stays pure and testable - it must not make a
    network call from inside a scheduling decision.
    """
    marker = COMMAND_MODEL.get(command_name, "__no_model__")

    if marker == "__generation__":
        key = "generation"
        cost = model_footprint(key, generation_bytes)
        if not cost:
            # Unknown generation model size. Held, not admitted: a wrong
            # guess here is an OOM.
            return JobFootprint(uncertain=True)
        return JobFootprint(models={key: cost})

    if marker == "__embedding__":
        key = "embedding"
        cost = model_footprint(key, embedding_bytes)
        if not cost:
            return JobFootprint(uncertain=True)
        return JobFootprint(models={key: cost})

    # Any other marker names a model class whose size is stated outright -
    # `synthesis` today, `__no_model__` for everything that needs none. The
    # lookup is by marker rather than by a hardcoded 64 MB because the previous
    # form charged every unlisted command kind the cost of a *placeholder*, so
    # a class with a real model behind it would have been billed as plumbing.
    cost = EXPLICIT_FOOTPRINTS.get(marker, EXPLICIT_FOOTPRINTS["__no_model__"])
    return JobFootprint(models={marker: cost})


def merge_footprints(parts: Iterable[JobFootprint]) -> FootprintEstimate:
    """Combine per-job footprints into one resident-set estimate."""
    merged: Dict[str, int] = {}
    uncertain = False
    for part in parts:
        uncertain = uncertain or part.uncertain
        for key, cost in part.models.items():
            merged[key] = max(merged.get(key, 0), cost)
    estimate = FootprintEstimate(models=merged)
    if uncertain:
        # Surfaced through an explicit sentinel rather than a magic zero cost,
        # so a caller that forgets to check still sees a name it cannot
        # mistake for a real model.
        estimate.models.setdefault("__unknown__", 0)
    return estimate
