> Current packaged build: **2.0.2-hotfix2** (Railway build-context fix: `sql/` and `scripts/` included in Docker build context).

# model-relay V2

`model-relay` V2 是对 `model-relay_0903` 的增量重构实现。核心模型从“Job + Provider wire payload”升级为：

```text
Material -> Session -> Job -> Provider Adapter / History Codec
```

目标是保留原有 Postgres durable queue、Lease/Heartbeat、Checkpoint + Idempotency 恢复基础，同时完成：官方 Moonshot/Kimi 独立接入、Canonical Material 接管、统一 V2 Session/Job API、Provider-native History Codec、Raw Error 保真、Worker fencing，以及 Core 与 Dify/Fusion 解耦。

## 存储边界

```text
Railway private Storage Bucket
  └─ 新上传文件 / 必要输入视觉资产的 canonical bytes

Supabase Postgres
  ├─ relay_jobs / relay_sessions / Lease / Heartbeat
  ├─ relay_materials / ingestions / bindings / request keys
  └─ raw error metadata

Supabase Storage
  └─ request / response / history / raw error / provider-derived data / legacy Fusion artifacts
```

没有把 Supabase Postgres 或既有执行归档迁移到 Railway。

## 主要入口

```text
app/api.py          V1 compatibility + V2 API
app/api_v2_app.py   Core-only V2 API（不导入 Dify/Fusion）
app/worker.py       V2 Core Worker
app/material_worker.py URL Material Ingress Worker
app/legacy_worker.py   旧 V1/Fusion Worker
```

V2 API：

```text
POST /v2/materials
GET/POST/DELETE /v2/materials/{id}...
POST /v2/sessions
GET  /v2/sessions/{id}
POST /v2/sessions/{id}/jobs
GET  /v2/sessions/{id}/jobs/{job_id}
GET  /v2/sessions/{id}/jobs/{job_id}/result
POST /v2/sessions/{id}/jobs/{job_id}/cancel
GET  /v2/errors/{error_id}
GET  /v2/errors/{error_id}/raw
GET  /v2/capabilities
```

旧 `/v1/jobs` 与 `/v1/dify/relay` 保留兼容读取/执行入口。V2 Core 不依赖这些旧合同。

## Provider profiles

内置服务端 profile：

- `moonshot-official-chat`：官方 Moonshot Chat Completions；独立 `MOONSHOT_API_KEY`。
- `grok-aihubmix-responses`：保留既有 Grok/AIHubMix Responses 通道，材料从 Railway canonical object 签发短期读取 URL。
- `gemini-native`：Native `generateContent` + Files API/fileData。

V2 Registry 按 `upstream_profile` 精确选 Adapter，不对未知 provider/model 做万能 fallback。Provider profile 不接受客户端提供任意 base URL 或 Provider Key。

## 可靠性变化

- Material 只有在 staging -> immutable canonical -> hash/size readback -> DB publish 全部完成后才进入 `ready`。
- Session 固定 provider / account_scope / protocol / codec / material set；Job 不能静默追加未绑定材料。
- 幂等使用 owner + operation scope + Idempotency-Key，并校验 logical request fingerprint。
- append Session 在 Job 受理时原子占位，避免两次模型都执行以后才发现历史冲突。
- V2 Worker 使用 `execution_engine + lease_token` fencing；失去 Lease 的 Worker 不能提交历史/终态。
- Provider 响应已归档但 DB 未提交时，重领 Worker 从归档继续提交，不再次调用模型。
- 已 `dispatch_started` 但无法证明响应是否完成时，记录 `delivery_status=unknown`，禁止自动重发。
- Provider Binding 有 DB lease/fencing，避免并发 Worker 同时首次准备同一厂商文件/传输凭据。

## Raw Error

V2 不把 Provider 错误统一改写为 `UPSTREAM_BAD_REQUEST`，也不再用 compact-result 截断器处理 Raw Error。原始 Body、headers、HTTP status、content encoding、byte length、SHA-256 和完整性状态归档在 Supabase；大错误可通过 `/v2/errors/{id}/raw` 读取。

## 数据库

已有 0903 环境先执行：

```text
sql/003_relay_v2_schema.sql
```

它是 additive migration：新增 Material/Error/Binding/Request-Key 表与 V2 RPC，扩展 `relay_sessions` / `relay_jobs`，并隔离 legacy/v2 Worker claim。

## 快速部署

见 [`QUICKSTART_V2.md`](QUICKSTART_V2.md)。

## 测试与验证边界

本包在生成时通过 Python compileall 与 28 个单元测试。由于当前环境没有你的 Railway Bucket、Supabase 实例和 Provider 生产凭据，数据库 migration 以及 Moonshot/Gemini/Grok 的真实网络合同需要在部署环境继续执行验收矩阵。架构实现不能替代部署账户的 endpoint/model/file/schema 能力验证。

### Railway build identity / boto3 hardening (2.0.3-hotfix3)
The Docker build now prints its build identity and installs boto3/botocore in a dedicated layer before installing the full requirements file. This makes a stale or mismatched Railway build context immediately visible and prevents Material S3 support from depending on a single requirements-file installation path.
