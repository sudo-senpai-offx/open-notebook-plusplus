"""Tiny from-scratch trainer (T5-a1 core, promoted from the prototype).

Char-level GPT, sub-20M params, torch CPU. Entry is gated
(`gate_training("tiny_train")` — denied raises InvalidInputError, which the
worker treats as permanent). Torch imports lazily inside the run so every
non-training boot pays nothing. Runs are step- and time-capped; classroom
runs stay bounded by tier. Subprocess isolation and worker wiring are a2.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from open_notebook.exceptions import InvalidInputError
from open_notebook.utils.capability_tier import gate_training

DEFAULT_MAX_STEPS = 200
HARD_MAX_STEPS = 2000
DEFAULT_MAX_SECONDS = 300
BLOCK_SIZE = 128
BATCH_SIZE = 16


def _build_model(vocab: int, n_layer: int = 4, n_embd: int = 128, n_head: int = 4):
    import torch
    import torch.nn as nn

    torch.set_num_threads(1)
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:
        pass  # one-call-only per process; a prior call already pinned it

    class Block(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.ln1 = nn.LayerNorm(n_embd)
            self.attn = nn.MultiheadAttention(n_embd, n_head, batch_first=True)
            self.ln2 = nn.LayerNorm(n_embd)
            self.mlp = nn.Sequential(
                nn.Linear(n_embd, 4 * n_embd),
                nn.GELU(),
                nn.Linear(4 * n_embd, n_embd),
            )

        def forward(self, x):  # type: ignore[no-untyped-def]
            h = self.ln1(x)
            attn_out, _ = self.attn(h, h, h, need_weights=False)
            x = x + attn_out
            return x + self.mlp(self.ln2(x))

    class TinyGPT(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.tok = nn.Embedding(vocab, n_embd)
            self.pos = nn.Embedding(BLOCK_SIZE, n_embd)
            self.layers = nn.Sequential(*[Block() for _ in range(n_layer)])
            self.ln = nn.LayerNorm(n_embd)
            self.head = nn.Linear(n_embd, vocab, bias=False)

        def forward(self, idx):  # type: ignore[no-untyped-def]
            _, t = idx.shape
            x = self.tok(idx) + self.pos(torch.arange(t, device=idx.device))
            return self.head(self.ln(self.layers(x)))

    return TinyGPT()


def run_tiny_train(
    text: str,
    *,
    max_steps: int = DEFAULT_MAX_STEPS,
    max_seconds: int = DEFAULT_MAX_SECONDS,
    progress: Optional[Callable[[int, float], None]] = None,
) -> Dict[str, Any]:
    """Train a tiny char-LM on `text`. Returns loss/steps/params/wall stats."""
    allowed, reason = gate_training("tiny_train")
    if not allowed:
        raise InvalidInputError(f"tiny_train denied by tier gate: {reason}")
    if not text or not text.strip():
        raise InvalidInputError("tiny_train needs non-empty text")
    steps = max(1, min(int(max_steps), HARD_MAX_STEPS))

    import torch
    import torch.nn as nn

    torch.set_num_threads(1)
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:
        pass  # one-call-only per process; a prior call already pinned it

    chars: List[str] = sorted(set(text))
    stoi = {c: i for i, c in enumerate(chars)}
    data = torch.tensor([stoi[c] for c in text], dtype=torch.long)
    n = len(data)
    if n < BLOCK_SIZE + 2:
        raise InvalidInputError(
            f"tiny_train needs at least {BLOCK_SIZE + 2} characters, got {n}"
        )

    model = _build_model(len(chars))
    params = sum(p.numel() for p in model.parameters())
    opt = torch.optim.AdamW(model.parameters(), lr=3e-4)
    model.train()

    t0 = time.time()
    loss_value = float("nan")
    done = 0
    for step in range(1, steps + 1):
        if time.time() - t0 >= max_seconds:
            break
        ix = torch.randint(0, n - BLOCK_SIZE - 1, (BATCH_SIZE,))
        x = torch.stack([data[i : i + BLOCK_SIZE] for i in ix])
        y = torch.stack([data[i + 1 : i + BLOCK_SIZE + 1] for i in ix])
        logits = model(x)
        loss = nn.functional.cross_entropy(logits.view(-1, logits.size(-1)), y.view(-1))
        opt.zero_grad()
        loss.backward()
        opt.step()
        loss_value = loss.item()
        done = step
        if progress is not None:
            progress(step, loss_value)

    return {
        "loss": loss_value,
        "steps": done,
        "params": params,
        "wall_s": round(time.time() - t0, 1),
    }
