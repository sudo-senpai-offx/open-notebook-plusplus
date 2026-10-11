from typing import ClassVar, cast

from pydantic import Field

from open_notebook.domain.base import RecordModel


class ServiceState(RecordModel):
    """Runtime service state. Never config: policy lives in env, the live
    draining signal lives here so the API, the governor, and the lease guard
    share one source. Cleared on every API boot so a stale flag from a
    previous run can never hang the next one.
    """

    record_id: ClassVar[str] = "open_notebook:service_state"
    draining: bool = Field(
        False,
        description="True while the stack is hibernating/draining. Set by the "
        "governor, cleared on boot.",
    )

    async def set_draining(self, value: bool) -> "ServiceState":
        self.draining = value
        await self.update()
        return self

    async def is_draining(self) -> bool:
        instance = cast("ServiceState", await ServiceState.get_instance())
        return bool(instance.draining)

    @classmethod
    async def clear_on_boot(cls) -> None:
        """Idempotent boot-clear: a persisted draining=true from a crashed run
        must not survive into the next boot.

        Runs in API lifespan only: the worker package init executes before its
        DB connection exists, so it cannot clear there. Both programs boot
        together under supervisord, so the API clear covers every boot.
        """
        instance = cast("ServiceState", await cls.get_instance())
        if instance.draining:
            instance.draining = False
            await instance.update()
