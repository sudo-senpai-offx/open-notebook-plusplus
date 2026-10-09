"""Subprocess isolation helpers for heavy operations."""

import asyncio
import os
import shlex
from typing import Any, Dict, Optional


async def run_heavy_operation(
    cmd: list[str],
    cwd: Optional[str] = None,
    timeout: Optional[int] = None,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Run heavy operation in isolated subprocess with timeout."""
    effective_env = dict(os.environ)
    if env:
        effective_env.update(env)
    # Conservative defaults
    effective_env.setdefault("TOKENIZERS_PARALLELISM", "false")
    effective_env.setdefault("OMP_NUM_THREADS", "1")
    effective_env.setdefault("MKL_NUM_THREADS", "1")
    effective_env.setdefault("NUMEXPR_NUM_THREADS", "1")
    effective_env.setdefault("OPENBLAS_NUM_THREADS", "1")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=effective_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return {
            "returncode": proc.returncode,
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
        }
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        raise


def to_cmd(args: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in args)
