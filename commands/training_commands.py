import time
from typing import Optional

from loguru import logger
from surreal_commands import CommandInput, CommandOutput, command

from open_notebook.exceptions import InvalidInputError
from open_notebook.training.tiny import (
    DEFAULT_MAX_SECONDS,
    DEFAULT_MAX_STEPS,
    run_tiny_train_isolated,
)
from open_notebook.utils.job_control import (
    Heartbeat,
    JobCancelledError,
    get_current_command_id,
    raise_if_cancelled,
    report_progress_record,
)


class TrainTinyInput(CommandInput):
    text: str
    name: str = "tiny-run"
    max_steps: int = DEFAULT_MAX_STEPS
    max_seconds: int = DEFAULT_MAX_SECONDS


class TrainTinyOutput(CommandOutput):
    success: bool
    name: str
    loss: float = float("nan")
    steps: int = 0
    params: int = 0
    processing_time: float = 0.0
    error_message: Optional[str] = None


@command(
    "train_tiny",
    app="open_notebook",
    retry={
        "max_attempts": 3,
        "wait_strategy": "exponential_jitter",
        "wait_min": 1,
        "wait_max": 60,
        "stop_on": [
            JobCancelledError,
            InvalidInputError,
            ValueError,
        ],  # Gate denials and bad inputs are permanent; child
        # crashes/timeouts are transient and may retry.
        "retry_log_level": "warning",
    },
)
async def train_tiny_command(input_data: TrainTinyInput) -> TrainTinyOutput:
    """Train a tiny char-LM on inline text, isolated in a child process.

    Text in, stats out: nothing durable is written. The heartbeat thread
    keeps writing while the child trains.
    """
    start_time = time.time()
    cmd_id = get_current_command_id() or (
        str(input_data.execution_context.command_id)
        if input_data.execution_context
        else None
    )
    hb = Heartbeat(cmd_id)
    hb.start()
    try:
        await report_progress_record(cmd_id, progress=5, stage="starting")
        await raise_if_cancelled(cmd_id)
        logger.info(
            f"Starting tiny training run '{input_data.name}' "
            f"(max_steps={input_data.max_steps})"
        )

        await report_progress_record(cmd_id, progress=20, stage="training")
        stats = await run_tiny_train_isolated(
            input_data.text,
            max_steps=input_data.max_steps,
            max_seconds=input_data.max_seconds,
        )

        await report_progress_record(cmd_id, progress=100, stage="completed")
        return TrainTinyOutput(
            success=True,
            name=input_data.name,
            loss=float(stats["loss"]),
            steps=int(stats["steps"]),
            params=int(stats["params"]),
            processing_time=time.time() - start_time,
        )
    except InvalidInputError:
        raise
    except Exception as e:
        logger.debug(f"Transient tiny-training failure for '{input_data.name}': {e}")
        raise
    finally:
        try:
            hb.stop()
        except Exception:
            pass
