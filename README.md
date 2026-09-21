# Model Relay 0.5.1 - Default-All Connections + Clear Route Diagnostics

本版本以 `model-relay-v2 0.5.0` 为基线，修复 connection 可用性与 route 错误诊断问题，同时保留 0.5.0 的路由解耦和上传日志能力。

1. **路由解耦**：Dify 只表达 `provider + model`，Relay 服务端通过 `RouteResolver / RouteCatalog` 解析并冻结内部 `connection_id`。Gemini 在 Railway Relay 上会自动进入 `GeminiNativeAdapter + GeminiAIHubMixFileAdapter`；Kimi 使用 `MoonshotChatAdapter + KimiOfficialFileAdapter`；Grok 使用 `ResponsesV2Adapter`。
2. **文件上传日志补齐**：完整覆盖 JSON/multipart 解析、参数/policy 校验、Dify/HTTPS source fetch、Material Registry、Provider Files API、Supabase fallback 与 API 最终成功/失败。


## 0.5.1 关键变化

- 默认 `CONNECTION_AVAILABILITY_MODE=all`：旧 `ENABLED_CONNECTIONS` 不再默认限制 Route；即使部署环境仍保留 `ENABLED_CONNECTIONS=aihubmix_default`，只要 Gemini 所需服务端配置存在，`provider=gemini + gemini-*` 仍会解析到 Gemini Native route。
- `RouteCatalog` 定义“有哪些合法 route”，不再由 enabled allowlist 决定 route 是否存在；连接凭据、Adapter 注册和未来 allowlist 是独立的可用性检查。
- 未配置凭据时返回 `ROUTE_CONNECTION_NOT_CONFIGURED`，Adapter 缺失时返回 `ROUTE_ADAPTER_NOT_REGISTERED` / `ROUTE_FILE_ADAPTER_NOT_REGISTERED`，不再误报 `ROUTE_NOT_FOUND`。
- `route_resolution_failed` 日志增加 `reason_detail / connection_policy / connection_enabled / connection_configured / configuration_reason / provider_model_patterns / registered_*_connections`。
- 如未来确实需要限制连接，显式设置 `CONNECTION_AVAILABILITY_MODE=allowlist` 后才使用 `ENABLED_CONNECTIONS`。

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
