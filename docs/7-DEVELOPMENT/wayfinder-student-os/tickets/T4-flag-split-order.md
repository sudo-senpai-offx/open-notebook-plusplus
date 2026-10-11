# T4 — Flag-Split Order (ALLOW_TINY_TRAIN / ALLOW_LORA)

Label: `wayfinder:task` (AFK-capable). Parent: `../map.md`. Blocks: nothing.
Blocked by: T2 (needs tier predicate first).

## Question

Implement the flag split only after T2 locks the tier predicate: keep
`MINI_ONLY` as inference-routing (existing `test_flag_defaults.py` untouched),
add `ALLOW_TINY_TRAIN` + `ALLOW_LORA` default false in
`open_notebook/ai/model_policy.py`, pin them false-by-default with new tests
mirroring the existing opt-in pattern, wire tier-gating at each training entry
point, and document in `.env.example`. One flag per change, full battery green
each. Do not start until T2 is closed.
