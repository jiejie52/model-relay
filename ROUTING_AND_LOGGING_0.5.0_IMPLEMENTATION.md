# Route Decoupling + Upload Logging Implementation Map

## 路由

- `app/routing/catalog.py`
  - deployment-local RouteCatalog
  - built-in mapping derived from enabled connections
  - optional `ROUTE_CATALOG_JSON`
  - revision + catalog hash
- `app/routing/route_resolver.py`
  - `provider/model/purpose -> RouteBinding`
  - `ROUTE_NOT_FOUND / ROUTE_MODEL_UNSUPPORTED / ROUTE_CONNECTION_DISABLED / ROUTE_CONFIG_INVALID`
  - legacy hint warn/strict behavior
  - startup catalog validation
- `app/api_v2/router.py`
  - Material/Session 在服务端 resolve route
  - Request 从 Session 注入 provider/model/connection
  - Session metadata 冻结 `_relay_route`
  - Material 与 Session route mismatch Fail-Closed / fallback rebind
- `app/core/execution_runtime.py`
  - Request snapshot 与 Session frozen route 断言
  - frozen material binding connection/account-scope 断言

## 文件上传日志

API 入口在取得 `material_id` 前先生成 `ingress_id`，因此 JSON/multipart、owner/policy、source URL 等早期失败也可串联。

```text
material_upload_received
  -> material_request_parse_failed       # JSON/multipart
  -> material_request_validation_failed  # owner/purpose/route intent
  -> route_resolved / route_resolution_failed
  -> material_ingress_started
  -> material_policy_validation_failed
  -> material_source_fetch_started
     -> completed / failed
  -> material_registry_create_failed
  -> material_route_bound
  -> provider_file_binding_started
     -> completed / failed
  -> material_fallback_store_started
     -> completed / failed
  -> material_ingress_completed
  -> material_upload_completed / failed  # API final event
```

`material_source_fetch_failed` 不打印 URL query 或响应正文；原始 source/provider 错误仍通过响应/detail 或 Raw Error 机制保真。

## 迁移模式

默认：

```text
ROUTE_LEGACY_HINT_MODE=warn
```

旧客户端仍可发送 connection 字段，但它们只能作为 assertion/hint。若 client hint 与 Relay route 不一致，日志记录 `legacy_connection_hint_mismatch`，执行仍使用 Relay route。

完成 Dify 瘦身后：

```text
ROUTE_LEGACY_HINT_MODE=strict
```

此时 legacy mismatch 返回 409。
