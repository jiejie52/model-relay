# V2 implementation notes — 2026-09-18

Base: `model-relay_0903.rar` (SHA-256 `cfd13bf15824aa9ce5382fe17ea276f59c9ab02261752fccb35eb995a54316f3`).

Implemented against `Relay_官方Kimi接入与解耦改造方案_V2_20260917`:

- Added `/v2/materials`, `/v2/sessions`, V2 Session Jobs, Raw Error and capability APIs with `relay-envelope/2.0`.
- Added Railway S3-compatible `UploadedFileStore`; existing Supabase execution archive remains separate.
- Added two-phase Material publish, SHA-256/size verification, URL SSRF guards and durable URL-ingress worker.
- Added Material/Session/Job/request-key/error/provider-binding tables and V2 Postgres RPCs in `sql/003_relay_v2_schema.sql`.
- Added `execution_engine` separation and fencing tokens; legacy claim now only takes `legacy`, V2 claim only takes configured `v2` engine.
- Added V2 atomic Session Job reservation and history/job terminal commit.
- Added archived-response recovery and `delivery_status=unknown` fail-closed handling after uncertain dispatch.
- Added exact server-side provider profiles and independent official Moonshot/Kimi Chat adapter.
- Added V2 Grok Responses and Gemini native adapters, provider-native history codecs, material bindings and binding fencing.
- Added Raw Error byte preservation and `/raw` delivery. Old V1 result endpoint now exposes `raw_error_id`/raw reference instead of relying only on a safe summary flag.
- Moved original business-aware Worker to `app.legacy_worker`; `app.worker` is Core-only and does not import Fusion/Dify modules.
- Kept original Fusion runtime/data path for compatibility; Dify DSL is intentionally not modified by this package.

Verification performed while packaging:

```text
python -m compileall -q app tests
python -m unittest discover -s tests -v
=> 28 tests, OK
```

Not verified in the packaging environment: live Railway S3, Supabase migration execution, and real Moonshot/Gemini/Grok account contracts. Run deployment-account contract tests before enabling production traffic.

## 2.0.1-hotfix1 — 2026-09-18

Deployment hotfix after Railway startup log showed `ModuleNotFoundError: No module named 'boto3'` while importing `app/storage/uploaded_files.py`.

- Keep `boto3` and `botocore` explicit in `requirements.txt`.
- Docker build now verifies `boto3`, `botocore`, FastAPI and core runtime imports before producing the image.
- S3 client import is guarded so an alternative/misconfigured build no longer crashes Uvicorn at module-import time.
- API health now reports whether Material Storage is configured/ready and a non-secret error string when it is unavailable.
- Added `scripts/preflight_runtime.py` for Railway dependency/configuration checks without printing secret values.
- Added a regression test proving the storage module remains importable when `boto3` is absent.

The V2 Material API still requires a working S3 client and `MATERIAL_S3_*` configuration; the guard only prevents the entire API process from dying before health/diagnostics are available.

## 2.0.2-hotfix2 — 2026-09-18

Railway build hotfix after the builder reported `CopyIgnoredFile` for `sql` and then failed at `COPY sql ./sql` because `.dockerignore` excluded that directory.

- Removed `sql` from `.dockerignore`, so `sql/001_relay_schema.sql`, `002_fusion_runtime.sql`, and `003_relay_v2_schema.sql` are present in the Docker build context.
- Dockerfile now copies `scripts/` as well as `app/` and `sql/`, so the documented `python scripts/preflight_runtime.py` command is actually available inside the deployed image.
- Added a build-stage runtime preflight, Python bytecode compile, and imports of both `app.api` and `app.api_v2_app`; packaging/import failures now stop the image build before deployment.
- Added packaging regression tests for `.dockerignore` and Dockerfile copy/preflight rules.

This hotfix changes deployment packaging only; Relay V2 API/Worker/provider semantics are unchanged from hotfix1.

Packaging verification for hotfix2: `python -m pytest -q` => **31 passed**; build-time dependency preflight and application import smoke test also passed locally. A Docker daemon was not available in the packaging environment, so the final Railway image build itself remains to be verified by deployment.

## 2.0.3-hotfix3 — Railway boto3 build-proof installation

Observed Railway failure: dependency installation completed, but the build-time import check still raised `ModuleNotFoundError: No module named 'boto3'`.

Changes:
- `Dockerfile` now prints a unique `2.0.3-hotfix3` build marker and the exact `requirements.txt` seen by Railway.
- `boto3` and `botocore` are installed in their own Docker `RUN` step before the general requirements installation.
- The same two packages remain in `requirements.txt` intentionally, so the Docker build no longer depends on one mechanism only.
- Build logs run `pip show boto3 botocore` and import both packages immediately after installation.

Expected successful build markers:
- `Build: 2.0.3-hotfix3`
- `material storage client OK ...`
- `runtime dependencies OK`
- `application imports OK`

If Railway does not print `Build: 2.0.3-hotfix3`, it is building a different root/source/revision than this package.

## 2.0.4-hotfix4 - Railway mixed-source dependency guard

Railway build logs for hotfix3 proved `BUILD_INFO.txt`/`Dockerfile` were from 2.0.3, while the `requirements.txt` visible inside the same build context did not contain `boto3`/`botocore`. The diagnostic `grep` therefore returned exit code 1 before Docker reached the explicit boto installation layer.

Changes:
- make the boto requirement diagnostic non-fatal;
- explicitly install `boto3`, `botocore`, and `python-multipart` in Dockerfile before `requirements.txt`;
- retain the complete canonical dependencies in the packaged `requirements.txt`;
- keep build marker, dependency import checks, preflight, compileall, and app import smoke tests.

If Railway prints `WARN: boto3/botocore not present in build-context requirements.txt`, the image can still build, but that warning proves the deployment source contains mixed versions and should be cleaned up after service recovery.

## 2.0.5-hotfix5 - Railway Storage Bucket runtime variable compatibility

Observed runtime failure after a successful image build: `app.worker` instantiated `UploadedFileStore` and failed with `Railway material storage is not configured; set MATERIAL_S3_* variables`.

Changes:
- preserve the explicit V2 `MATERIAL_S3_*` configuration contract;
- additionally accept Railway Bucket references `ENDPOINT/BUCKET/REGION/ACCESS_KEY_ID/SECRET_ACCESS_KEY`;
- additionally accept Railway CLI/AWS-compatible names `AWS_ENDPOINT_URL/AWS_S3_BUCKET_NAME/AWS_DEFAULT_REGION/AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY/AWS_S3_URL_STYLE`;
- explicit `MATERIAL_S3_*` values have highest precedence;
- default S3 addressing style is now `auto`, with explicit/`AWS_S3_URL_STYLE` override;
- Worker startup error reports missing logical fields without printing secret values;
- runtime preflight recognizes all supported naming schemes;
- added regression tests for explicit, Railway-reference, AWS-compatible, precedence, and missing-field behavior.

This hotfix does not weaken the V2 reliability boundary: the V2 Worker still refuses to run when no complete canonical Material store configuration is available.
