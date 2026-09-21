# Model Relay 0.5.3 - Relay-Authoritative Gemini Size

本版本以 `model-relay-v2 0.5.2` 为基线。Gemini 仍固定使用服务端 RouteResolver + `GeminiNativeAdapter`，但文件大小判断改为 **Relay 自己测量并在 Request 冻结时重新聚合**。调用方不再负责提供权威总字节数。

## 0.5.3 关键变化

- Relay 下载/接收材料后直接以实际 bytes 写入 `actual_size`；SHA-256 与 size 都由 Relay 计算。
- 缺少 `request_file_total_bytes` 时不再保守走 Gemini Files API。单文件 547024 bytes 这类场景会直接判定为 `<=99 MiB`，进入 Supabase Signed External URL。
- 真正调用模型前，Relay 对 Session + Request 的最终 `material_ids` 重新读取 `actual_size` 并求和。
- Request 总量 `<=99 MiB`：Supabase Private Bucket -> Signed URL -> `gemini_external_url` -> Gemini `fileData.fileUri`。
- Request 总量 `>99 MiB`：Gemini Files API -> `gemini_file_uri`。若多个小文件此前已有 External URL bridge，Relay 会在 dispatch 前升格到 Files API，并删除输入文件 Supabase 副本。
- 0.5.2 的 `request_file_total_bytes/request_file_count/material_batch_id` 与 `X-Relay-*` headers 继续兼容，但只用于诊断，不能覆盖 Relay 自己的实际 size。
- 无新增 SQL migration。

详细实现见 `GEMINI_RELAY_AUTHORITATIVE_SIZE_0.5.3_IMPLEMENTATION.md`。

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
