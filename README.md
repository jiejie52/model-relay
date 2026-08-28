# model-relay

Dify 的通用异步模型调用层。基础设施不绑定 Grok；当前首先处理 AIHubMix `/responses`、Grok `store=false`、`reasoning.encrypted_content`、无状态历史重放和 Dify 小结果回收，后续可以在 `app/providers/` 增加其他 Provider Adapter。

## 目标

```text
Dify
  -> POST /v1/jobs        (快速返回 202 + job_id)
  -> GET job status
  -> GET compact result   (严格小于 Dify 大响应边界)

Railway relay-api
  -> Supabase relay_jobs / relay_sessions

Railway relay-worker
  -> AIHubMix /responses
  -> Supabase Storage 保存 raw response / response.output / history
```

Dify 永远不应拉取 `raw-response.json`、完整 `response.output` 或 encrypted reasoning。

## 目录

```text
model-relay/
├── app/
│   ├── api.py
│   ├── worker.py
│   ├── config.py
│   ├── models.py
│   ├── repository.py
│   ├── security.py
│   ├── storage_paths.py
│   ├── supabase.py
│   ├── utils.py
│   └── providers/
│       ├── base.py
│       ├── openai_compatible.py
│       └── registry.py
├── sql/
│   └── 001_relay_schema.sql
├── Dockerfile
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

## 1. 先处理旧密钥

如果 AIHubMix Key 或 Supabase Secret 曾经出现在导出的 Dify DSL、聊天记录或代码仓库中，请先在服务端轮换，再部署本项目。不要把真实密钥写入本仓库。

## 2. Supabase

### 2.1 Storage

复用现有 Private Bucket，例如：

```text
SUPABASE_BUCKET=dify-assets
```

Relay 会自动写入：

```text
dify-assets/
  relay/
    <tenant_id>/
      <conversation_hash>/
        sessions/...
        jobs/...
```

这些原始对象不向 Dify 生成下载 URL。

### 2.2 Postgres

在 Supabase Dashboard -> SQL Editor 中运行：

```text
sql/001_relay_schema.sql
```

该 SQL 会创建：

```text
relay_jobs
relay_sessions
claim_relay_job(...)
renew_relay_job_lease(...)
commit_relay_session_history(...)
```

`claim_relay_job` 使用 `FOR UPDATE SKIP LOCKED`，后续可以安全扩多个 Worker Replica。

## 3. GitHub Desktop

1. File -> New repository
2. Name: `model-relay`
3. 把本项目所有文件复制到该仓库目录
4. Summary 填 `Initial model relay implementation`
5. Commit to main
6. Publish repository
7. 建议仓库保持 Private

## 4. Railway

创建 Project：

```text
model-relay-platform
```

同一 GitHub 仓库创建两个 Service。

### relay-api

Service 名：

```text
relay-api
```

Start Command 可以留给 Dockerfile 默认值，或显式填写：

```bash
uvicorn app.api:app --host 0.0.0.0 --port $PORT
```

生成 Public Domain，并设置 Healthcheck：

```text
/health
```

### relay-worker

Service 名：

```text
relay-worker
```

Start Command：

```bash
python -m app.worker
```

不要给 Worker 配公网域名。Worker 应保持长期运行，不要使用按请求唤醒的 Function 形态。

## 5. Railway 环境变量

`relay-api` 与 `relay-worker` 共用：

```text
AIHUBMIX_API_KEY
AIHUBMIX_OPENAI_BASE_URL=https://aihubmix.com/v1
SUPABASE_URL
SUPABASE_SECRET_KEY
SUPABASE_BUCKET=dify-assets
SUPABASE_SIGNED_URL_TTL=604800
RELAY_API_TOKEN
WORKER_MAX_RUNTIME_SECONDS=2400
JOB_LEASE_SECONDS=120
JOB_HEARTBEAT_SECONDS=30
RELAY_RESULT_SOFT_LIMIT_BYTES=524288
RELAY_RESULT_HARD_LIMIT_BYTES=786432
RELAY_RESULT_PREVIEW_BYTES=307200
```

推荐使用 Railway Shared Variables。

`SUPABASE_SECRET_KEY` 推荐使用后端 `sb_secret_...`。代码对新 secret key 只发送 `apikey` Header；如果仍使用 legacy `service_role` JWT，则兼容发送 `Authorization: Bearer`。

## 6. API

所有业务 API 都要求：

```http
Authorization: Bearer <RELAY_API_TOKEN>
```

查询、结果和取消还要求：

```http
X-Tenant-Id: <tenant_id>
X-Conversation-Hash: <conversation_hash>
```

这样 Job 不能只凭 `job_id` 被其他会话读取。

### 6.1 提交 Job

```http
POST /v1/jobs
Idempotency-Key: <stable-key>
```

普通 Grok 新材料会话示例：

```json
{
  "tenant_id": "dify-app-id",
  "conversation_hash": "sha256-of-dify-conversation-id",
  "stage": "normal_inference",
  "provider": "grok",
  "model": "grok-4.6",
  "think_level": "xhigh",
  "mode": "new_session",
  "current_query": "用户问题",
  "material_prefix": [
    {
      "role": "user",
      "content": [
        {"type": "input_text", "text": "材料前缀示例"}
      ]
    }
  ],
  "material_prefix_includes_current_query": false,
  "upstream": {
    "base_url": "https://aihubmix.com/v1"
  }
}
```

返回：

```json
{
  "job_id": "uuid",
  "relay_session_id": "uuid",
  "status": "queued",
  "poll_after_seconds": 5
}
```

### 6.2 后续追问

```json
{
  "tenant_id": "dify-app-id",
  "conversation_hash": "sha256-of-dify-conversation-id",
  "relay_session_id": "此前返回的 session uuid",
  "stage": "normal_inference",
  "provider": "grok",
  "model": "grok-4.6",
  "think_level": "xhigh",
  "mode": "continue_session",
  "current_query": "新的追问"
}
```

Dify 不再发送完整 encrypted history。Worker 会从 Supabase Storage 加载不可变材料前缀和完整历史。

如果你在改 DSL 时直接复用现有 `grok_material_prefix_json`，而该 JSON 已经包含首轮问题，则首轮提交必须设置：

```json
{"material_prefix_includes_current_query": true}
```

这样 Relay 不会重复追加首轮问题；保存历史时也不会把同一首轮 user item 再写一次。

### 6.3 Fusion / 单次无状态 Job

Fusion Comparison、Decision、Final Plan、Quality、Repair 可以使用：

```text
mode=stateless
```

并使用通用 stage：

```text
fusion_comparison
fusion_decision
fusion_final_plan
fusion_quality
fusion_repair
```

不要把 stage 命名成 `grok_*`。

### 6.4 查询状态

```http
GET /v1/jobs/{job_id}
Authorization: Bearer ...
X-Tenant-Id: ...
X-Conversation-Hash: ...
```

### 6.5 获取 Dify 精简结果

```http
GET /v1/jobs/{job_id}/result?view=dify
```

返回只包含业务正文、usage、response_id、session/job 状态等小字段。

明确不返回：

```text
reasoning.encrypted_content
完整 response.output
原始 response body
不可变材料前缀
完整历史数组
```

当可见正文自身超过 soft limit 时，Relay 会把完整正文保存在 Storage，并只给 Dify 返回预览。

### 6.6 取消

```http
POST /v1/jobs/{job_id}/cancel
```

运行中的上游请求未必能真正中断，但 Worker 完成后会再次检查 Job 是否已取消，不会把迟到结果覆盖成 succeeded。

## 7. Dify 环境变量

Dify 新增：

```text
RELAY_BASE_URL=https://<relay-api-domain>
RELAY_API_TOKEN=<same-token-as-Railway>
RELAY_ENABLED=true
RELAY_SHORT_POLL_SECONDS=60
RELAY_RESULT_MAX_BYTES=786432
```

Dify 不应在每次请求中传：

```text
AIHUBMIX_API_KEY
SUPABASE_SECRET_KEY
```

## 8. Dify 推荐通用节点名

```text
model_relay_router
model_job_request_builder
model_job_submit_http
model_job_submit_parser
assign_model_job_state
model_job_short_poll_loop
model_job_status_http
model_job_result_http
model_job_response_adapter
model_job_resume_router
answer_model_job_pending
```

会话变量推荐：

```text
relay_session_id
relay_last_job_id
relay_pending_action
relay_checkpoint_json
relay_material_expires_at
relay_provider
relay_model
```

## 9. 幂等键

Dify 应生成稳定的 `Idempotency-Key`，例如：

```text
SHA256(
  app_id
  + conversation_id
  + action
  + fusion_session_id
  + fusion_version
  + normalized_request_hash
)
```

数据库通过 `(tenant_id, idempotency_key)` 唯一索引防止重复计费。

## 10. 当前 Provider 行为

`app/providers/openai_compatible.py` 当前：

- 调用 `<base_url>/responses`
- Relay 自己管理历史，因此 `store=false`
- Grok 自动加入 `include=["reasoning.encrypted_content"]`
- Grok `low/medium/high/xhigh` 映射为 `reasoning.effort`
- `auto` 不显式发送 reasoning effort
- Grok Session 使用稳定 `prompt_cache_key`
- 完整 `response.output` 保存在 Supabase Storage
- 后续追问按完整 item 重放，不截断单个 encrypted reasoning 字符串

以后增加其他模型时，优先在 `app/providers/` 增加 Adapter，不复制 API、Worker、Job 表或 Railway 服务。

## 11. 本地启动（可选）

复制：

```bash
cp .env.example .env
```

安装：

```bash
pip install -r requirements.txt
```

API：

```bash
uvicorn app.api:app --reload --port 8000
```

Worker：

```bash
python -m app.worker
```

## 12. 最小验收

1. `/health` 返回 200。
2. `POST /v1/jobs` 数秒内返回 queued/job_id，不等待模型完成。
3. 模拟 5 MB encrypted reasoning，Supabase Storage 存完整 Raw Response，Dify 结果仍小于 hard limit。
4. 模拟 15-30 分钟模型调用，API 不维持长连接，Worker 可继续执行。
5. Worker 崩溃后 lease 到期可重新领取任务。
6. 同一 Idempotency-Key 不重复创建模型调用。
7. `continue_session` 只向 Dify 暴露 session id，不暴露 encrypted history。
8. Relay 不可用时不要让 Dify 自动回退到直连大响应模型调用。

## 7. Provider-Neutral Fusion Runtime (v0.2.0-fusion)

This package now accepts the same `/v1/jobs` control plane for answer-fusion jobs.
Normal `normal_inference` session behavior is unchanged.

Supported Fusion stages:

```text
fusion_corpus_ingest
material_evidence_mapping
global_adjudication
scoped_decision
final_evidence_review
direct_final_synthesis
synthesis_blueprint
final_draft_generation
quality_review
evidence_grounded_repair
```

### 7.1 Database migration

After the existing `001_relay_schema.sql`, run:

```text
sql/002_fusion_runtime.sql
```

It creates `fusion_corpora`, `fusion_materials` and `fusion_artifacts`.
The Relay API/Worker continues to be the only backend component that reads/writes these tables.

### 7.2 Fusion Job contract

Fusion jobs are stateless at the Relay session layer and are linked by immutable
`fusion_corpus_id` plus versioned Artifact IDs. `fusion_corpus_ingest` also accepts
`mode=new_fusion_corpus` for compatibility with the Dify DSL.

Example Corpus ingest:

```json
{
  "tenant_id": "dify-app-id",
  "conversation_hash": "sha256-of-conversation",
  "stage": "fusion_corpus_ingest",
  "provider": "gemini",
  "model": "gemini-3.1-flash-lite",
  "think_level": "medium",
  "mode": "new_fusion_corpus",
  "fusion_corpus_id": "fcor_xxx",
  "payload": {
    "corpus_version": 1,
    "business_question": "...",
    "materials": []
  }
}
```

Model stages use the same Job endpoint and submit `fusion_corpus_id`, `stage`,
`route_profile` and a stage-specific `payload`. The Worker rehydrates the Corpus
and referenced Artifacts from Supabase before calling the provider.

### 7.3 Storage boundary

Canonical Corpus and Artifacts are stored below:

```text
dify-assets/fusion/<tenant>/<conversation>/<corpus_id>/...
```

Dify receives only compact job results, Corpus IDs, Artifact IDs and stage payloads.
Raw provider responses remain in Relay job storage.
