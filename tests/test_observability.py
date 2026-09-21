import asyncio
import json
import logging
from types import SimpleNamespace
import unittest

from app.observability import configure_logging, info as log_info
from app.providers.http_wire import read_raw_response


class _BrokenResponse:
    status_code = 200
    headers = {
        "content-type": "application/json",
        "content-length": "100",
        "x-request-id": "up-stream-1",
    }
    request = SimpleNamespace(method="POST", url="https://provider.example/v1/responses?secret=no-log")

    async def aiter_raw(self):
        yield b"abc"
        raise ConnectionResetError("provider closed stream")


class ObservabilityTests(unittest.TestCase):
    def test_dependency_polling_loggers_are_suppressed_by_default(self):
        settings = SimpleNamespace(
            log_level="INFO",
            dependency_http_log_level="WARNING",
            uvicorn_access_log=False,
        )
        configure_logging(settings)
        self.assertEqual(logging.getLogger("httpx").level, logging.WARNING)
        self.assertEqual(logging.getLogger("httpcore").level, logging.WARNING)
        self.assertEqual(logging.getLogger("uvicorn.access").level, logging.WARNING)

    def test_structured_event_redacts_secrets(self):
        logger = logging.getLogger("test-observability-redaction")
        logger.setLevel(logging.INFO)
        with self.assertLogs(logger, level="INFO") as captured:
            log_info(
                logger,
                "request_test",
                request_id="req_1",
                relay_api_token="must-not-appear",
                detail={"authorization": "Bearer secret", "safe": "ok"},
            )
        payload = json.loads(captured.output[0][captured.output[0].find("{"):])
        self.assertEqual(payload["request_id"], "req_1")
        self.assertEqual(payload["relay_api_token"], "[redacted]")
        self.assertNotIn("authorization", payload["detail"])
        self.assertEqual(payload["detail"]["safe"], "ok")
        self.assertNotIn("must-not-appear", captured.output[0])

    def test_stream_interruption_is_logged_with_partial_byte_count(self):
        async def run():
            response = _BrokenResponse()
            try:
                await read_raw_response(
                    response,
                    log_context={"request_id": "req-stream", "provider": "gemini"},
                )
            except ConnectionResetError as exc:
                return exc
            self.fail("stream interruption did not raise")

        with self.assertLogs("model-relay-upstream", level="ERROR") as captured:
            exc = asyncio.run(run())
        joined = "\n".join(captured.output)
        self.assertIn('"event":"upstream_stream_interrupted"', joined)
        self.assertIn('"request_id":"req-stream"', joined)
        self.assertIn('"bytes_received":3', joined)
        self.assertIn('"http_status":200', joined)
        self.assertIn('"upstream_path":"/v1/responses"', joined)
        self.assertNotIn("secret=no-log", joined)
        self.assertTrue(getattr(exc, "stream_interrupted", False))
        self.assertEqual(getattr(exc, "bytes_received", None), 3)


if __name__ == "__main__":
    unittest.main()
