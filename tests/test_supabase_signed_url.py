import unittest

from app.supabase import _normalize_supabase_signed_url


class SupabaseSignedUrlNormalizationTests(unittest.TestCase):
    ROOT = "https://project-ref.supabase.co"

    def test_storage_relative_object_sign_path_gets_storage_v1_prefix(self):
        signed = "/object/sign/dify-assets/path/file.pdf?token=abc"
        self.assertEqual(
            _normalize_supabase_signed_url(self.ROOT, signed),
            "https://project-ref.supabase.co/storage/v1/object/sign/dify-assets/path/file.pdf?token=abc",
        )

    def test_storage_rooted_path_does_not_duplicate_storage_v1(self):
        signed = "/storage/v1/object/sign/dify-assets/path/file.pdf?token=abc"
        self.assertEqual(
            _normalize_supabase_signed_url(self.ROOT + "/", signed),
            "https://project-ref.supabase.co/storage/v1/object/sign/dify-assets/path/file.pdf?token=abc",
        )

    def test_absolute_signed_url_is_preserved(self):
        signed = "https://cdn.example.test/object.pdf?token=abc"
        self.assertEqual(_normalize_supabase_signed_url(self.ROOT, signed), signed)

    def test_relative_without_leading_slash_uses_storage_base(self):
        signed = "object/sign/dify-assets/path/file.pdf?token=abc"
        self.assertEqual(
            _normalize_supabase_signed_url(self.ROOT, signed),
            "https://project-ref.supabase.co/storage/v1/object/sign/dify-assets/path/file.pdf?token=abc",
        )

    def test_empty_signed_url_fails_closed(self):
        with self.assertRaises(RuntimeError):
            _normalize_supabase_signed_url(self.ROOT, "")


if __name__ == "__main__":
    unittest.main()
