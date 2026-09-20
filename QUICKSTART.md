# Relay v2 关键操作指示

本目录是基于 `model-relay_0903` 的改造版。主协议改为 **Session -> Request -> optional Job**；`/v1/jobs` 继续作为兼容入口。

## 1. 数据库升级

先确认旧版本 SQL 已执行，再按顺序执行：

```text
sql/001_relay_schema.sql
sql/002_fusion_runtime.sql          # 仍需兼容旧 Fusion 时执行
sql/003_relay_v2_session_request_material.sql
```

`003` 新增 `relay_requests / relay_materials / relay_objects / provider_material_bindings`，并给 `relay_jobs` 增加 `request_id / execution_pool / lease_epoch / protocol_version`。

## 2. 环境变量

从 `.env.example` 复制。两地必须使用同一版本代码/镜像，只改配置。

Railway 示例：

```text
DEPLOYMENT_ID=railway
EXECUTION_POOL=railway-default
WORKER_EXECUTION_POOLS=railway-default
ENABLED_CONNECTIONS=aihubmix_default
AIHUBMIX_API_KEY=...
DEFAULT_STORAGE_ID=supabase_shared
```

阿里云 SAE 示例：

```text
DEPLOYMENT_ID=aliyun-sae
EXECUTION_POOL=aliyun-default
WORKER_EXECUTION_POOLS=aliyun-default
ENABLED_CONNECTIONS=moonshot_official
MOONSHOT_API_KEY=...
DEFAULT_STORAGE_ID=supabase_shared
```

首期两边继续共用同一 Supabase Postgres + Storage。不要让两个 Worker 使用相同 `EXECUTION_POOL`，除非确实希望它们共同消费同一类任务。

## 3. 启动

API：

```bash
uvicorn app.api:app --host 0.0.0.0 --port 8000
```

Worker：

```bash
python -m app.worker
```

健康检查：`GET /health`。

## 4. 新主流程

1. `POST /v2/materials`：先把临时 URL/上传文件持久化，取得 `material_id`。
2. `POST /v2/sessions`：创建 Session，固定 provider/connection/model/context policy/material base。
3. `POST /v2/sessions/{session_id}/requests`：每轮请求都创建 Request；`execution.mode=sync` 不建 Job，`async` 才建 Job。
4. 查询：`GET .../requests/{request_id}` 或 `/result`。
5. 错误原文：失败 Request 的 Error Envelope 直接包含完整 `body_text`（可严格解码时）和精确 `body_base64`；`GET .../error/raw` 仍可读取 Storage 中归档的原始字节。不做 `UPSTREAM_*` 归类或摘要。
6. `indeterminate`：表示 Provider 是否完成未知；系统不会自动再建 Job 重放。先查询/对账，必要时显式取消该 Request 后再发起新的业务请求。

所有 Request 查询/取消仍要求：

```text
Authorization: Bearer <RELAY_API_TOKEN>
X-Tenant-Id: <tenant>
X-Conversation-Hash: <conversation_hash>
```

创建 Request 还必须提供稳定的 `Idempotency-Key`。

## 5. 官方 Kimi

新连接名：`moonshot_official`，Adapter：`app/providers/moonshot_chat.py`。

- 普通聊天走官方 Chat Completions。
- 图片使用持久化 Material 读取后转 Base64 wire payload。
- 文本文档/一般文档使用 file-extract 绑定；Provider file binding 失效后会从持久化 Material 重建。
- `provider_payload.material_mode=vision` 时，若当前材料只能走文本抽取，会 Fail-Closed，不会静默丢弃版式/视觉证据。
- Structured Output 在 Provider wire 层适配，返回后仍使用调用方 canonical JSON Schema 校验。

上线前必须用目标 Kimi 模型做真实集成测试，尤其验证文件上传/提取、图像输入、结构化输出、推理参数和原始错误返回；这些能力会随具体模型/官方协议变化。

## 6. 兼容入口

`/v1/jobs`、旧 `job_id` 查询/取消仍保留；旧 Fusion 执行被隔离在 `app/compatibility/legacy_worker.py`。新 v2 core/Provider 执行不再按 Fusion stage 分支。

注意：兼容不包含旧的 Error 归一化策略。依赖 `UPSTREAM_BAD_REQUEST/UPSTREAM_SERVER_ERROR` 的调用方需要同步升级。

## 7. 上线前最少验收

```bash
python -m unittest discover -s tests -v
```

重点再人工/集成验证：同步超时不创建第二个 Job、同键不同请求返回冲突、Worker 在 Provider dispatch 后崩溃进入 `indeterminate`、`result_stored` 后崩溃只完成 commit 不重新推理、Railway/SAE 不跨 pool 误领、原始错误 hash 与上游 body 一致。


## 0.3.0 -> 0.3.1 快速升级

本补丁不修改数据库表结构，不需要重新执行 `003`。替换代码/镜像后同时重启 Relay API 与 Worker，然后访问 `/health`，确认版本为：

```text
0.3.1-session-request-material-error-passthrough
```

业务侧重放一个会触发 Provider 4xx 的测试请求，返回 `error.body_size` 应与 `error.body_text` UTF-8 字节数一致（文本 JSON 场景），并可将 `error.body_base64` 解码回完全相同的原始响应字节。
