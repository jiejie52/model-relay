from __future__ import annotations

import asyncio

from ..config import Settings
from ..core.execution_runtime import SharedExecutionRuntime
from ..core.raw_error import RawErrorRecorder
from ..v2_repository import RelayV2Repository
from .errors import error_source


class InlineExecutor:
    def __init__(
        self,
        runtime: SharedExecutionRuntime,
        errors: RawErrorRecorder,
        repo: RelayV2Repository,
        settings: Settings,
    ) -> None:
        self.runtime = runtime
        self.errors = errors
        self.repo = repo
        self.settings = settings

    async def execute(self, request_row: dict):
        try:
            await asyncio.wait_for(
                self.runtime.execute(request_row),
                timeout=self.settings.sync_request_deadline_seconds,
            )
        except asyncio.TimeoutError as exc:
            err = await self.errors.record(
                exc=exc,
                source="relay_timeout",
                tenant_id=request_row["tenant_id"],
                conversation_hash=request_row["conversation_hash"],
                session_id=request_row["session_id"],
                request_id=request_row["id"],
            )
            # Provider side execution may already have happened. Do not create an
            # async job and do not pretend the provider did not receive it.
            await self.repo.fail_request(
                request_id=request_row["id"],
                session_id=request_row["session_id"],
                error=err,
                status="indeterminate",
                release_session=False,
            )
        except Exception as exc:
            err = await self.errors.record(
                exc=exc,
                source=error_source(exc),
                tenant_id=request_row["tenant_id"],
                conversation_hash=request_row["conversation_hash"],
                session_id=request_row["session_id"],
                request_id=request_row["id"],
            )
            await self.repo.fail_request(
                request_id=request_row["id"],
                session_id=request_row["session_id"],
                error=err,
                status="failed",
                release_session=True,
            )
        return await self.repo.get_request(request_row["id"])
