# Model Relay V2 — 简短操作指示

## 1. 数据库先升级

已有 `model-relay_0903` 数据库：在 Supabase SQL Editor 执行：

```text
sql/003_relay_v2_schema.sql
```

全新数据库：依次执行：

```text
sql/001_relay_schema.sql
sql/002_fusion_runtime.sql      # 需要保留旧 Fusion Runtime 时执行
sql/003_relay_v2_schema.sql
```

**必须先执行 003，再启动 V2 Worker。** 003 会把旧 `claim_relay_job` 限定为 `legacy`，并新增 V2 的 engine/fencing claim，避免新旧 Worker 混领任务。

## 2. 配置环境变量

复制 `.env.example`，至少配置：

```text
RELAY_API_TOKEN
RELAY_ALLOWED_TENANT_ID         # 单租户 token 强烈建议设置
SUPABASE_URL
SUPABASE_SECRET_KEY
SUPABASE_BUCKET
MATERIAL_S3_ENDPOINT
MATERIAL_S3_BUCKET
MATERIAL_S3_ACCESS_KEY_ID
MATERIAL_S3_SECRET_ACCESS_KEY
```

接 Kimi 再配置：

```text
MOONSHOT_API_KEY
MOONSHOT_API_ORIGIN=https://api.moonshot.ai/v1
```

Grok / Gemini 按需配置 `AIHUBMIX_API_KEY` / `GEMINI_API_KEY`。Kimi 不复用 AIHubMix Key。

## 3. 启动服务

安装依赖：

```bash
pip install -r requirements.txt
```

API（V1 兼容 + V2）：

```bash
uvicorn app.api:app --host 0.0.0.0 --port 8000
```

V2 Job Worker：

```bash
python -m app.worker
```

Material URL 接入 Worker：

```bash
python -m app.material_worker
```

如果还有旧 `/v1/jobs` / Fusion 在途任务，同时保留 Legacy Worker：

```bash
python -m app.legacy_worker
```

只想启动完全不导入 Dify/Fusion 的 V2 Core API，可用：

```bash
uvicorn app.api_v2_app:app --host 0.0.0.0 --port 8000
```

## 4. 调用顺序

V2 新调用按以下顺序：

```text
POST /v2/materials                  -> 等待 ready
POST /v2/sessions                   -> 固定 provider/profile/material set
POST /v2/sessions/{id}/jobs         -> 202 + job_id
GET  /v2/sessions/{id}/jobs/{job}
GET  /v2/sessions/{id}/jobs/{job}/result
```

所有写操作使用稳定 `Idempotency-Key`，并携带：

```text
Authorization: Bearer <RELAY_API_TOKEN>
X-Tenant-Id: <tenant>
X-Conversation-Hash: <conversation_hash>
```

已有 `job_id` 的恢复只查询 status/result，不重新 submit。

## 5. 上线前

本包已通过本地 Python 编译和单元测试；**Moonshot/Kimi、Gemini、Grok 的真实 endpoint、账户能力、文件能力和 Schema 能力仍必须用你的部署账户做合同测试**。不要用真实生产请求做重复发送式灰度测试。

## Railway startup hotfix / preflight

If deployment logs contain `ModuleNotFoundError: No module named 'boto3'`, the runtime image was built without all dependencies. This package includes `boto3`/`botocore` and a Docker build-time import check.

Deploy from the directory that contains `Dockerfile` and `requirements.txt` (the root of this package). Railway should detect the root `Dockerfile`. If the service is configured with a custom Root Directory, point it at this package root. After build, run once in a shell or as a temporary start command:

```bash
python scripts/preflight_runtime.py
```

Then restore the API start command:

```bash
uvicorn app.api:app --host 0.0.0.0 --port ${PORT:-8000}
```

`GET /health` now includes `material_storage.configured`, `material_storage.ready`, and a non-secret diagnostic string. `ready=false` means Material endpoints must not be opened to traffic yet; V1 compatibility endpoints can still start for diagnosis.

## Railway build-context hotfix 2.0.2

If Railway shows `CopyIgnoredFile: ... "sql" ... excluded by .dockerignore` followed by `COPY sql ./sql` / `"/sql": not found`, deploy **2.0.2-hotfix2 or newer**. In this build, `sql/` is no longer ignored and `scripts/` is copied into the image.

A successful Docker build should pass these image-build checks before Uvicorn starts:

```text
runtime dependencies OK
Model Relay V2 runtime preflight
...
application imports OK
```

Do not add `sql` or `scripts` back to `.dockerignore` while the Dockerfile contains `COPY sql ./sql` / `COPY scripts ./scripts`.

## Railway boto3 hotfix 2.0.3

If the Docker build reaches the dependency check but still reports `ModuleNotFoundError: No module named 'boto3'`, use **2.0.3-hotfix3 or newer**. This build installs `boto3`/`botocore` twice by design: first explicitly from the Dockerfile and then as part of `requirements.txt`.

At the start of the Docker build, confirm the log contains:

```text
=== MODEL RELAY BUILD MARKER ===
Build: 2.0.3-hotfix3
```

Then confirm:

```text
material storage client OK ...
runtime dependencies OK
application imports OK
```

If the first marker is missing, do not debug Python dependencies yet: Railway is building another directory/revision. Check the service Source and Root Directory so the directory containing this package's `Dockerfile`, `BUILD_INFO.txt`, and `requirements.txt` is the build root.

### Railway hotfix4 build note
If the build log shows `Build: 2.0.4-hotfix4` but the printed `requirements.txt` does not list `boto3`/`botocore`, hotfix4 will no longer fail at the diagnostic step. It explicitly installs `boto3`, `botocore`, and `python-multipart` before installing `requirements.txt`.

A healthy build should later print both `material storage/runtime clients OK` and `application imports OK`. A `WARN: boto3/botocore not present...` line means the Railway source tree is mixed-version; deploy the zip as a clean replacement rather than overlaying individual files when convenient.
