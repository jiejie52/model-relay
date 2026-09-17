import asyncio
from types import SimpleNamespace
import unittest
from uuid import uuid4

from app.core_models import SessionJobRequest
from app.core_service import RelayCoreService, canonical_hash
from app.providers.registry import ProviderRegistry


class _Repo:
    def __init__(self, session, job):
        self.session = session
        self.job = job

    async def get_session(self, session_id, **kwargs):
        return self.session

    async def find_job_by_idempotency(self, tenant_id, conversation_hash, key):
        return self.job


class _Backend:
    pass


class CoreIdempotencyTests(unittest.TestCase):
    def test_retry_attaches_to_original_job_after_session_advanced(self):
        session_id = uuid4()
        session = {
            "id": str(session_id),
            "tenant_id": "tenant",
            "conversation_hash": "conv",
            "provider": "moonshot",
            "model": "kimi-k3",
            "upstream_profile_id": "moonshot-official",
            "protocol": "openai-chat-completions",
            "history_codec": "moonshot-chat/1",
            "history_mode": "append",
            # The session has advanced well beyond the original request.
            "history_version": 5,
            "expires_at": None,
        }
        request = SessionJobRequest(
            input=[{"role": "user", "content": "same request"}],
            model="kimi-k3",
            generation={"reasoning": {"effort": "high"}},
        )
        original_version = 1
        fp = canonical_hash(
            {
                "session_id": str(session_id),
                "provider": "moonshot",
                "upstream_profile": "moonshot-official",
                "protocol": "openai-chat-completions",
                "history_codec": "moonshot-chat/1",
                "history_mode": "append",
                "expected_history_version": original_version,
                "model": "kimi-k3",
                "input": request.input,
                "generation": request.generation,
                "provider_payload": request.provider_payload,
                "structured_output": request.structured_output,
                "metadata": request.metadata,
                "label": request.label,
            }
        )
        existing = {
            "id": str(uuid4()),
            "relay_session_id": str(session_id),
            "expected_history_version": original_version,
            "model": "kimi-k3",
            "request_fingerprint": fp,
            "status": "succeeded",
        }
        repo = _Repo(session, existing)
        settings = SimpleNamespace()
        service = RelayCoreService(
            _Backend(), repo, ProviderRegistry(settings), settings
        )
        result = asyncio.run(
            service.submit_session_job(
                session_id,
                request,
                tenant_id="tenant",
                conversation_hash="conv",
                idempotency_key="stable-key",
            )
        )
        self.assertIs(result, existing)


if __name__ == "__main__":
    unittest.main()
