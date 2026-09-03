from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .callback_contract import CALLBACK_KIND
from .config import Settings
from .repository import RelayRepository

logger = logging.getLogger("model-relay-callback")


class DifyCallbackDispatcher:
    def __init__(self, repo: RelayRepository, settings: Settings, worker_id: str) -> None:
        self.repo = repo
        self.settings = settings
        self.worker_id = worker_id
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=settings.dify_callback_connect_timeout_seconds,
                read=settings.dify_callback_timeout_seconds,
                write=60.0,
                pool=30.0,
            )
        )

    async def close(self) -> None:
        await self.client.aclose()

    def configured(self) -> bool:
        return bool(
            self.settings.dify_callback_enabled
            and str(self.settings.dify_callback_base_url or "").strip()
            and self.settings.dify_callback_api_key is not None
        )

    async def run_forever(self) -> None:
        logger.info("callback_dispatcher_started worker_id=%s", self.worker_id)
        while True:
            try:
                job = await self.repo.claim_callback(self.worker_id)
                if not job:
                    await asyncio.sleep(self.settings.dify_callback_poll_seconds)
                    continue
                await self.deliver(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("callback_dispatcher_loop_error")
                await asyncio.sleep(self.settings.dify_callback_poll_seconds)

    async def deliver(self, job: dict[str, Any]) -> None:
        job_id = str(job.get("id") or "")
        if str(job.get("callback_kind") or "") != CALLBACK_KIND:
            await self.repo.fail_callback(
                job_id,
                self.worker_id,
                "UNSUPPORTED_CALLBACK_KIND",
                terminal=True,
            )
            return

        if not self.configured():
            await self._retry_or_fail(job, "DIFY_CALLBACK_NOT_CONFIGURED")
            return

        conversation_id = str(job.get("callback_conversation_id") or "").strip()
        user_id = str(job.get("callback_user_id") or "").strip()
        query = str(job.get("callback_resume_query") or "").strip()
        if not conversation_id or not user_id or not query:
            await self.repo.fail_callback(
                job_id,
                self.worker_id,
                "CALLBACK_REGISTRATION_INCOMPLETE",
                terminal=True,
            )
            return

        url = str(self.settings.dify_callback_base_url or "").rstrip("/") + "/chat-messages"
        api_key = self.settings.dify_callback_api_key.get_secret_value()  # type: ignore[union-attr]
        body = {
            "inputs": {},
            "query": query,
            "response_mode": "blocking",
            "conversation_id": conversation_id,
            "user": user_id,
        }
        try:
            resp = await self.client.post(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "X-Relay-Callback-Job-Id": job_id,
                },
                json=body,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            await self._retry_or_fail(job, f"DIFY_CALLBACK_NETWORK:{type(exc).__name__}:{exc}")
            return
        except Exception as exc:
            await self._retry_or_fail(job, f"DIFY_CALLBACK_ERROR:{type(exc).__name__}:{exc}")
            return

        if 200 <= resp.status_code < 300:
            await self.repo.complete_callback(job_id, self.worker_id)
            logger.info(
                "callback_delivered job_id=%s stage=%s status=%s",
                job_id,
                job.get("stage"),
                resp.status_code,
            )
            return

        excerpt = str(resp.text or "")[:2000]
        await self._retry_or_fail(
            job,
            f"DIFY_CALLBACK_HTTP_{resp.status_code}:{excerpt}",
        )

    async def _retry_or_fail(self, job: dict[str, Any], error: str) -> None:
        attempt = int(job.get("callback_attempt_count") or 0)
        terminal = attempt >= self.settings.dify_callback_max_attempts
        if terminal:
            await self.repo.fail_callback(str(job.get("id") or ""), self.worker_id, error, terminal=True)
            logger.error("callback_failed_permanently job_id=%s error=%s", job.get("id"), error[:500])
            return
        delay = min(
            self.settings.dify_callback_max_backoff_seconds,
            self.settings.dify_callback_base_backoff_seconds * (2 ** max(0, attempt - 1)),
        )
        await self.repo.retry_callback(
            str(job.get("id") or ""),
            self.worker_id,
            error,
            delay_seconds=delay,
        )
        logger.warning(
            "callback_retry_scheduled job_id=%s attempt=%s delay=%ss error=%s",
            job.get("id"), attempt, delay, error[:500],
        )
