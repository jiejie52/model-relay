# Model Relay 0.5.2 - Gemini Dual Transport (Supabase External URL / Files API)

本版本以 `model-relay-v2 0.5.1` 为基线，保持服务端 RouteResolver、Default-All connections、完整上传日志与 Raw Error 能力，并把 Gemini 输入文件改为按**本次模型请求全部文件原始字节总和**选择材料传输方式。

## 0.5.2 关键变化

- Gemini route 始终保持 `GeminiNativeAdapter`，不会因为文件大小切换到 generic Responses Adapter。
- 当前请求文件总量 `<= 99 MiB (103809024 bytes)`：原始文件写入 Supabase Private Bucket，Relay 生成 Signed URL，Provider binding 记为 `gemini_external_url`，推理时作为 Gemini `fileData.fileUri`。
- 当前请求文件总量 `> 99 MiB`：原始文件不写 Supabase input-file object，继续使用 AIHubMix Gemini Native Proxy -> Gemini Files API -> `fileUri`。
- Supabase Signed URL TTL 复用现有 `SUPABASE_SIGNED_URL_TTL`，默认 `604800` 秒（7 天）；URL 过期时若 Relay fallback object 仍存在，会重新签发 URL，不重新上传 Gemini Files API。
- 多文件阈值按 Request 聚合值判断，不按单文件分别判断。Material API 新增 `request_file_total_bytes / request_file_count / material_batch_id`，也支持对应 `X-Relay-*` Headers。
- 聚合总量缺失且无法确定为单文件时，Relay Fail-Safe 地选择 Gemini Files API，避免把实际 >99 MiB 的多文件请求错误走 External URL。
- `>99 MiB` Files API 路径会抑制 input-file Supabase fallback，即使旧客户端仍带 `relay_backed/always`，也不会把原始文件写 Supabase。
- 无新增 SQL migration。

## 关键边界

- `/v2/materials` 新合同使用 `provider/model/purpose`，不再要求 `target_connection_id`。
- `/v2/sessions` 新合同使用 `provider/model`，不再要求 `connection_id`。
- `/v2/sessions/{session_id}/requests` 从 Session 读取已冻结 route；Request 不再决定 provider/model/connection。
- 为兼容旧 Dify，`connection_id/target_connection_id` 仍可暂时提交，但只作为 legacy hint。默认 `ROUTE_LEGACY_HINT_MODE=warn`：不一致时记录日志并忽略客户端 connection。`strict` 时返回 409。
- 新 Session 把 `route_revision/route_binding_hash/account_scope_hash/connection_id` 写入内部 Session metadata；业务响应不暴露内部 connection。
- 既有 Session **不自动重路由**。如果旧 Session 本身是错误路由，例如 `gemini + aihubmix_default`，应新建 Session。
- Storage fallback 只影响“文件字节是否落 Relay Storage”，不会改变 Provider Wire 或 Inference Adapter。

## 新增关键日志

文件上传链：

```text
material_upload_received
material_request_parse_failed
material_request_validation_failed
material_policy_validation_failed
material_source_fetch_started
material_source_fetch_completed / material_source_fetch_failed
material_registry_create_failed
material_route_bound
provider_file_binding_started / completed / failed
material_fallback_store_started / completed / failed
material_ingress_completed
material_upload_completed / material_upload_failed
```

路由链：

```text
route_catalog_validated
route_resolved
route_resolution_failed
legacy_connection_hint_mismatch
material_route_bound
session_route_frozen
legacy_request_route_hint_mismatch
request_route_snapshot_mismatch
route_binding_mismatch
```

所有关键错误都尽量带：`ingress_id/material_id/request_id/session_id/job_id`、`provider/model`、内部 `connection_id`（仅日志）、`route_revision`、`phase`、`duration_ms`、`http_status/upstream_http_status`、`failure_class`、`exception_type`。异常路径保留 traceback。

## 数据库

**0.5.0 -> 0.5.1 没有新增 SQL migration。** 路由冻结信息使用现有 `relay_sessions.metadata` 与 Request snapshot 保存。数据库仍需已完成 0.4.0 的 `sql/004_provider_native_file_ingress.sql`。

详细部署与调用示例见 `QUICKSTART.md`。
