# T5 — Tiny-Recipe Prototype (Fixed Arch, Measured)

Label: `wayfinder:prototype` (HITL logic prototype). Parent: `../map.md`.
Blocks: nothing. Blocked by: T2 (needs tier gate to know where it may run).

## Verdict (2026-10-11, prototype branch `prototype/T5-tiny-train`, NOT merged)

Run on dev box, single thread pinned: char-level 4x128x4 (~830K params),
TinyStories 2MB slice (vocab 78), batch 16, ctx 128, AdamW 3e-4, 300 steps:
loss 4.553 -> 2.373 (learning), wall 93s, peak RSS 468MB (incl. torch
runtime). Recipe WORKS and loss falls; footprint fits the 2GB tier with
headroom. Honest extrapolation: this box far outruns Pentium D class, so
budget ~5-10x wall there — short demo runs fit class time, long runs do
not. Scope B (custom-text import) affordable: same pipeline, user text in.
Prototype stays on its branch (`fad8b08`); no trainer in main.

## Question

Build the throwaway trainer that answers "does fixed-recipe tiny-from-scratch
feel right on this box?": MiniGPT-style sub-20M (config-dataclass model + KV
cache, on-device BPE, TinyStories sample), single shareable run script with
trivial start, in-memory state, loss curve surfaced, no persistence beyond a
clearly-marked `PROTOTYPE-wipe-me` dir. Measure wall time + peak RSS on this
machine and record whether scope B (custom-text import) is affordable. Capture
the validated verdict here, commit the prototype to a throwaway branch with a
context pointer, keep only the decision on main. Gated behind T2's tier
predicate; parent process keeps heartbeating via
`open_notebook/utils/subprocess_utils.py` patterns.
