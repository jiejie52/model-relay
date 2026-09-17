from pathlib import Path
from types import SimpleNamespace
import unittest

from app.providers.base import ProviderRequestError
from app.providers.registry import ProviderRegistry


ROOT = Path(__file__).resolve().parents[1]


class CoreArchitectureTests(unittest.TestCase):
    def test_core_worker_does_not_import_fusion_runtime(self):
        source = (ROOT / "app" / "worker.py").read_text()
        self.assertNotIn("fusion_runtime", source)
        self.assertNotIn("FUSION_STAGES", source)

    def test_core_repository_does_not_own_fusion_tables(self):
        source = (ROOT / "app" / "repository.py").read_text()
        self.assertNotIn("fusion_corpora", source)
        self.assertNotIn("create_fusion_", source)

    def test_v2_core_api_has_no_dify_route(self):
        source = (ROOT / "app" / "core_api.py").read_text()
        self.assertNotIn("/v1/dify", source)
        self.assertNotIn("view=dify", source)

    def test_unknown_provider_fails_closed(self):
        registry = ProviderRegistry(SimpleNamespace())
        with self.assertRaises(ProviderRequestError):
            registry.get("unknown-provider")


if __name__ == "__main__":
    unittest.main()
