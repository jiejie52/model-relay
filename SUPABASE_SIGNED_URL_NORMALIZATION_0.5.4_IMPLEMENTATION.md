# Model Relay 0.5.4 — Supabase Signed URL Normalization

## Problem

0.5.3 correctly calculated the authoritative Gemini Request total and selected `supabase_external_url` for a 547024-byte PDF. The provider then returned HTTP 400 `URL_ERROR-ERROR_NOT_FOUND` because Relay had normalized a relative Supabase `signedURL` against the project root instead of the Storage API root.

The broken behavior was effectively:

```text
SUPABASE_URL + /object/sign/...
```

The required behavior is:

```text
SUPABASE_URL + /storage/v1 + /object/sign/...
```

## Reference behavior

`WF-NormalInference_20260910-NoComments.yml` builds:

```text
storage_base = root + /storage/v1
signed = storage_base + rel   # when Supabase returns a Storage-relative path
```

0.5.4 ports this rule into Relay's `SupabaseBackend`, with additional guards for already-absolute and already-Storage-rooted response forms.

## Code change

`app/supabase.py` adds `_normalize_supabase_signed_url()` and `storage_sign_read_url()` delegates all `signedURL/signedUrl/signed_url` response variants to it.

Normalization matrix:

| Supabase response | Relay result |
|---|---|
| `https://host/...` | unchanged |
| `/storage/v1/object/...` | `<root>/storage/v1/object/...` |
| `storage/v1/object/...` | `<root>/storage/v1/object/...` |
| `/object/sign/...` | `<root>/storage/v1/object/sign/...` |
| `object/sign/...` | `<root>/storage/v1/object/sign/...` |

Empty root/URL fails closed. Query strings/tokens are preserved byte-for-byte as part of the returned string.

## Scope

Unchanged:

- `<=99 MiB` -> Supabase External URL
- `>99 MiB` -> Gemini Files API
- Relay-authoritative material-size aggregation
- Signed URL TTL (default 604800 seconds)
- Session/Request binding freeze
- database schema

## Verification

Added 5 regression tests covering the response shapes above. Full test suite: **68 passed**. `compileall`, API import, and Worker import are also verified before packaging.
