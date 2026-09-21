import asyncio
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError
from fastapi import HTTPException
from starlette.requests import Request

from app.materials.ingress import MaterialIngress, MaterialIngressError
from app.api_v2.router import create_material
from app.materials.provider_files.registry import ProviderFileRegistry
from app.materials.safe_fetch import MaterialFetchError
from app.routing import RouteCatalog, RouteEntry, RouteResolutionError, RouteResolver
from app.config import Settings
from app.v2_models import MaterialCreateJSON, SessionCreateRequest, SessionRequestCreate, SessionResponse


class _ProviderRegistry:
    def __init__(self, rows):
        self.rows = rows

    def describe(self, connection_id):
        return self.rows.get(connection_id)


class _FileRegistry:
    def __init__(self, rows=None):
        self.rows = rows or {}

    def describe(self, connection_id):
        return self.rows.get(connection_id)

    def maybe_get(self, connection_id):
        return None


class _RepoBeforeCreate:
    async def find_material_by_idempotency(self, tenant_id, idempotency_key):
        return None


class _FallbackUnused:
    pass


class RoutingV21Tests(unittest.TestCase):
    def _settings(self, *, enabled=None, legacy_mode="warn"):
        enabled = set(enabled or {"aihubmix_default", "aihubmix_gemini_native", "moonshot_official"})
        return SimpleNamespace(
            deployment_id="railway",
            execution_pool="railway-default",
            enabled_connection_set=enabled,
            route_legacy_hint_mode=legacy_mode,
            connection_account_scope_hash=lambda connection_id: f"scope:{connection_id}",
        )

    def _resolver(self, *, enabled=None, legacy_mode="warn"):
        settings = self._settings(enabled=enabled, legacy_mode=legacy_mode)
        catalog = RouteCatalog(
            revision="relay-route-catalog/test-1",
            entries=[
                RouteEntry("gemini", "gemini-*", "aihubmix_gemini_native", deployment_id="railway", requires_file_adapter=True),
                RouteEntry("grok", "grok-*", "aihubmix_default", deployment_id="railway"),
                RouteEntry("kimi", "kimi-*", "moonshot_official", deployment_id="railway", requires_file_adapter=True),
            ],
        )
        providers = _ProviderRegistry(
            {
                "aihubmix_gemini_native": {"provider": "gemini", "adapter_version": "gemini-native-aihubmix/1"},
                "aihubmix_default": {"provider": "grok", "adapter_version": "responses-v2/1"},
                "moonshot_official": {"provider": "kimi", "adapter_version": "moonshot-chat/1"},
            }
        )
        files = _FileRegistry(
            {
                "aihubmix_gemini_native": {"provider": "gemini", "adapter_version": "gemini-aihubmix-files/1"},
                "moonshot_official": {"provider": "kimi", "adapter_version": "kimi-official-files/1"},
            }
        )
        return RouteResolver(settings=settings, catalog=catalog, providers=providers, provider_files=files)

    def test_gemini_provider_model_resolves_native_connection(self):
        resolver = self._resolver()
        binding = resolver.resolve(provider="gemini", model="gemini-3.1-flash-lite", purpose="session")
        self.assertEqual(binding.connection_id, "aihubmix_gemini_native")
        self.assertEqual(binding.inference_adapter_version, "gemini-native-aihubmix/1")
        self.assertEqual(binding.file_adapter_version, "gemini-aihubmix-files/1")

    def test_grok_and_kimi_resolve_independently(self):
        resolver = self._resolver()
        grok = resolver.resolve(provider="grok", model="grok-4.3", purpose="session")
        kimi = resolver.resolve(provider="kimi", model="kimi-k2.5", purpose="session")
        self.assertEqual(grok.connection_id, "aihubmix_default")
        self.assertEqual(kimi.connection_id, "moonshot_official")

    def test_legacy_wrong_connection_is_logged_and_ignored(self):
        resolver = self._resolver(legacy_mode="warn")
        binding = resolver.resolve(provider="gemini", model="gemini-3.1-flash-lite", purpose="session")
        with self.assertLogs("model-relay-routing", level="WARNING") as captured:
            resolver.handle_legacy_hint(
                client_hint="aihubmix_default",
                resolved=binding,
                caller_version="legacy-dify",
                scope="session",
            )
        self.assertIn("legacy_connection_hint_mismatch", "\n".join(captured.output))
        self.assertEqual(binding.connection_id, "aihubmix_gemini_native")

    def test_strict_legacy_mismatch_fails_closed(self):
        resolver = self._resolver(legacy_mode="strict")
        binding = resolver.resolve(provider="gemini", model="gemini-3.1-flash-lite", purpose="session")
        with self.assertRaises(RouteResolutionError) as ctx:
            resolver.handle_legacy_hint(
                client_hint="aihubmix_default",
                resolved=binding,
                scope="session",
            )
        self.assertEqual(ctx.exception.code, "LEGACY_CONNECTION_HINT_MISMATCH")

    def test_unsupported_model_fails_closed(self):
        resolver = self._resolver()
        with self.assertRaises(RouteResolutionError) as ctx:
            resolver.resolve(provider="gemini", model="not-a-gemini-model", purpose="session")
        self.assertEqual(ctx.exception.code, "ROUTE_MODEL_UNSUPPORTED")

    def test_disabled_route_fails_closed(self):
        resolver = self._resolver(enabled={"aihubmix_default"})
        with self.assertRaises(RouteResolutionError) as ctx:
            resolver.resolve(provider="gemini", model="gemini-3.1-flash-lite", purpose="session")
        self.assertEqual(ctx.exception.code, "ROUTE_CONNECTION_DISABLED")

    def test_default_connection_policy_ignores_legacy_enabled_connections_allowlist(self):
        settings = Settings(
            relay_api_token="relay-token",
            supabase_url="https://supabase.example",
            supabase_secret_key="service-key",
            enabled_connections="aihubmix_default",
            aihubmix_api_key="aihub-key",
            aihubmix_gemini_base_url="https://gemini-native.example",
        )
        self.assertEqual(settings.connection_availability_mode, "all")
        self.assertTrue(settings.connection_is_enabled("aihubmix_default"))
        self.assertTrue(settings.connection_is_enabled(settings.aihubmix_gemini_connection_id))
        self.assertTrue(settings.connection_is_enabled(settings.moonshot_connection_id))
        catalog = RouteCatalog.from_settings(settings)
        entry = catalog.match(
            provider="gemini",
            model="gemini-3.1-flash-lite",
            deployment_id=settings.deployment_id,
        )
        self.assertIsNotNone(entry)
        self.assertEqual(entry.connection_id, settings.aihubmix_gemini_connection_id)

    def test_missing_gemini_server_config_is_not_reported_as_route_not_found(self):
        settings = Settings(
            relay_api_token="relay-token",
            supabase_url="https://supabase.example",
            supabase_secret_key="service-key",
            enabled_connections="aihubmix_default",
            aihubmix_api_key="aihub-key",
            aihubmix_gemini_base_url=None,
        )
        catalog = RouteCatalog.from_settings(settings)
        resolver = RouteResolver(
            settings=settings,
            catalog=catalog,
            providers=_ProviderRegistry({}),
            provider_files=_FileRegistry({}),
        )
        with self.assertLogs("model-relay-routing", level="ERROR") as captured:
            with self.assertRaises(RouteResolutionError) as ctx:
                resolver.resolve(
                    provider="gemini",
                    model="gemini-3.1-flash-lite",
                    purpose="material_ingress",
                )
        self.assertEqual(ctx.exception.code, "ROUTE_CONNECTION_NOT_CONFIGURED")
        joined = "\n".join(captured.output)
        self.assertIn('"reason":"ROUTE_CONNECTION_NOT_CONFIGURED"', joined)
        self.assertIn("AIHUBMIX_GEMINI_BASE_URL", joined)
        self.assertNotIn('"reason":"ROUTE_NOT_FOUND"', joined)

    def test_allowlist_mode_can_still_disable_a_route_explicitly(self):
        settings = Settings(
            relay_api_token="relay-token",
            supabase_url="https://supabase.example",
            supabase_secret_key="service-key",
            connection_availability_mode="allowlist",
            enabled_connections="aihubmix_default",
            aihubmix_api_key="aihub-key",
            aihubmix_gemini_base_url="https://gemini-native.example",
        )
        catalog = RouteCatalog.from_settings(settings)
        providers = _ProviderRegistry({
            "aihubmix_gemini_native": {"provider": "gemini", "adapter_version": "gemini-native-aihubmix/1"},
        })
        files = _FileRegistry({
            "aihubmix_gemini_native": {"provider": "gemini", "adapter_version": "gemini-aihubmix-files/1"},
        })
        resolver = RouteResolver(
            settings=settings, catalog=catalog, providers=providers, provider_files=files
        )
        with self.assertRaises(RouteResolutionError) as ctx:
            resolver.resolve(provider="gemini", model="gemini-3.1-flash-lite", purpose="session")
        self.assertEqual(ctx.exception.code, "ROUTE_CONNECTION_DISABLED")

    def test_public_contract_does_not_require_connection(self):
        session = SessionCreateRequest(
            tenant_id="t",
            conversation_hash="c",
            provider="gemini",
            model="gemini-3.1-flash-lite",
        )
        self.assertIsNone(session.connection_id)
        request = SessionRequestCreate(input="hi", execution={"mode": "sync"})
        self.assertIsNone(request.connection_id)
        response_fields = SessionResponse.model_fields
        self.assertNotIn("connection_id", response_fields)

    def test_inference_material_requires_provider_and_model(self):
        with self.assertRaises(ValidationError):
            MaterialCreateJSON(
                tenant_id="t",
                conversation_hash="c",
                filename="a.pdf",
                content_base64="YQ==",
            )
        archive = MaterialCreateJSON(
            tenant_id="t",
            conversation_hash="c",
            filename="a.pdf",
            content_base64="YQ==",
            purpose="archive",
        )
        self.assertEqual(archive.purpose, "archive")

    def test_source_fetch_failure_has_structured_lifecycle_logs(self):
        settings = SimpleNamespace(
            material_default_durability_policy="native_first",
            material_default_fallback_policy="on_provider_unavailable",
            material_ingress_max_bytes=1024,
            material_ingress_timeout_seconds=1.0,
            material_allow_http=False,
        )
        ingress = MaterialIngress(_RepoBeforeCreate(), _FallbackUnused(), ProviderFileRegistry(), settings)
        failure = MaterialFetchError(
            "source returned forbidden",
            code="MATERIAL_SOURCE_HTTP_ERROR",
            phase="source_http",
            status_code=403,
            body=b"forbidden",
            response_headers={"content-type": "text/plain"},
            bytes_received=9,
        )

        async def run():
            with patch("app.materials.ingress.fetch_bytes", new=AsyncMock(side_effect=failure)):
                await ingress.create(
                    tenant_id="t",
                    conversation_hash="c",
                    idempotency_key="k",
                    filename="a.pdf",
                    content_type="application/pdf",
                    source_url="https://files.example/private-token/a.pdf?secret=hidden",
                    ingress_id="ing_test",
                )

        with self.assertLogs("model-relay-materials", level="INFO") as captured:
            with self.assertRaises(MaterialIngressError) as ctx:
                asyncio.run(run())
        joined = "\n".join(captured.output)
        self.assertIn('"event":"material_source_fetch_started"', joined)
        self.assertIn('"event":"material_source_fetch_failed"', joined)
        self.assertIn('"ingress_id":"ing_test"', joined)
        self.assertIn('"upstream_http_status":403', joined)
        self.assertNotIn("secret=hidden", joined)
        self.assertNotIn("private-token", joined)
        self.assertEqual(ctx.exception.detail["body_text"], "forbidden")

    def test_material_api_parse_failure_emits_final_failure_event(self):
        settings = SimpleNamespace(
            material_default_durability_policy="native_first",
            material_default_fallback_policy="on_provider_unavailable",
            material_ingress_max_bytes=1024,
        )
        app = SimpleNamespace(state=SimpleNamespace(settings=settings))
        body = b'{"broken":'
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v2/materials",
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"",
            "server": ("test", 80),
            "client": ("127.0.0.1", 12345),
            "scheme": "http",
            "app": app,
        }
        request = Request(scope, receive)

        with self.assertLogs("model-relay-api.v2", level="WARNING") as captured:
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(create_material(request, idempotency_key="idem"))
        self.assertEqual(ctx.exception.status_code, 400)
        joined = "\n".join(captured.output)
        self.assertIn('"event":"material_request_parse_failed"', joined)
        self.assertIn('"event":"material_upload_failed"', joined)

    def test_schema_validation_logs_do_not_echo_file_payload(self):
        settings = SimpleNamespace(
            material_default_durability_policy="native_first",
            material_default_fallback_policy="on_provider_unavailable",
            material_ingress_max_bytes=1024,
        )
        app = SimpleNamespace(state=SimpleNamespace(settings=settings))
        secret_payload = "VERY_SECRET_BASE64_PAYLOAD"
        body = (
            '{"tenant_id":"t","conversation_hash":"c",'
            '"content_base64":"' + secret_payload + '",'
            '"provider":"gemini","model":"gemini-3.1-flash-lite"}'
        ).encode("utf-8")
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v2/materials",
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"",
            "server": ("test", 80),
            "client": ("127.0.0.1", 12345),
            "scheme": "http",
            "app": app,
        }
        request = Request(scope, receive)

        with self.assertLogs("model-relay-api.v2", level="WARNING") as captured:
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(create_material(request, idempotency_key="idem-secret"))
        self.assertEqual(ctx.exception.status_code, 422)
        joined = "\n".join(captured.output)
        self.assertIn('"event":"material_request_parse_failed"', joined)
        self.assertNotIn(secret_payload, joined)
        self.assertNotIn(secret_payload, str(ctx.exception.detail))

    def test_material_ingress_error_emits_api_final_failure_event(self):
        settings = SimpleNamespace(
            material_default_durability_policy="native_first",
            material_default_fallback_policy="on_provider_unavailable",
            material_ingress_max_bytes=1024,
        )
        failure = MaterialIngressError(
            {
                "source": "material_source",
                "code": "MATERIAL_SOURCE_HTTP_ERROR",
                "phase": "source_http",
                "upstream_http_status": 403,
                "message": "forbidden",
            },
            status_code=502,
            ingress_id="ing_final",
        )
        app = SimpleNamespace(
            state=SimpleNamespace(
                settings=settings,
                material_ingress=SimpleNamespace(create=AsyncMock(side_effect=failure)),
            )
        )
        body = (
            '{"tenant_id":"t","conversation_hash":"c","filename":"a.pdf",'
            '"content_base64":"YQ==","purpose":"archive"}'
        ).encode("utf-8")
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v2/materials",
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"",
            "server": ("test", 80),
            "client": ("127.0.0.1", 12345),
            "scheme": "http",
            "app": app,
        }
        request = Request(scope, receive)

        with self.assertLogs("model-relay-api.v2", level="ERROR") as captured:
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(create_material(request, idempotency_key="idem-final"))
        self.assertEqual(ctx.exception.status_code, 502)
        joined = "\n".join(captured.output)
        self.assertIn('"event":"material_upload_failed"', joined)
        self.assertIn('"phase":"source_http"', joined)
        self.assertIn('"upstream_http_status":403', joined)

    def test_policy_validation_failure_is_logged(self):
        settings = SimpleNamespace(
            material_default_durability_policy="native_first",
            material_default_fallback_policy="on_provider_unavailable",
            material_ingress_max_bytes=1024,
            material_ingress_timeout_seconds=1.0,
            material_allow_http=False,
        )
        ingress = MaterialIngress(_RepoBeforeCreate(), _FallbackUnused(), ProviderFileRegistry(), settings)

        async def run():
            await ingress.create(
                tenant_id="t",
                conversation_hash="c",
                idempotency_key="k2",
                filename="a.pdf",
                content_type="application/pdf",
                data=b"x",
                fallback_policy="bad-policy",
                ingress_id="ing_policy",
            )

        with self.assertLogs("model-relay-materials", level="WARNING") as captured:
            with self.assertRaises(MaterialIngressError):
                asyncio.run(run())
        joined = "\n".join(captured.output)
        self.assertIn('"event":"material_policy_validation_failed"', joined)
        self.assertIn('"ingress_id":"ing_policy"', joined)


if __name__ == "__main__":
    unittest.main()
