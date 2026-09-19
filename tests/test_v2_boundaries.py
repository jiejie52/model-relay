from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
V2_FILES = [
    ROOT / "app/api_v2/router.py",
    ROOT / "app/core/execution_runtime.py",
    ROOT / "app/core/raw_error.py",
    ROOT / "app/execution/inline_executor.py",
    ROOT / "app/execution/queue_executor.py",
    ROOT / "app/materials/ingress.py",
    ROOT / "app/materials/resolver.py",
    ROOT / "app/providers/moonshot_chat.py",
    ROOT / "app/providers/responses_v2.py",
]


class V2BoundaryTests(unittest.TestCase):
    def test_v2_core_has_no_fusion_stage_branching(self):
        forbidden = [
            "FUSION_STAGES",
            "fusion_runtime",
            "global_adjudication",
            "final_draft_generation",
            "scoped_decision",
            "quality_review",
            "state_patch",
        ]
        merged = "\n".join(path.read_text(encoding="utf-8") for path in V2_FILES)
        for token in forbidden:
            self.assertNotIn(token, merged)

    def test_v2_error_path_has_no_upstream_error_classification(self):
        merged = "\n".join(path.read_text(encoding="utf-8") for path in V2_FILES)
        self.assertNotIn("UPSTREAM_BAD_REQUEST", merged)
        self.assertNotIn("UPSTREAM_SERVER_ERROR", merged)
        self.assertNotIn("compact_error_excerpt", merged)


if __name__ == "__main__":
    unittest.main()
