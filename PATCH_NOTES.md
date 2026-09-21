# Model Relay 0.5.0 Patch Notes

## Route contract 2.1

- 新增 `app/routing/catalog.py`、`app/routing/route_resolver.py`。
- `provider + model + local deployment` 由 Relay 解析为内部 `RouteBinding`。
- 新 Session 创建时冻结内部 connection；后续 Request 只使用 Session route。
- Material Ingress 与 Session/Inference 使用同一 route/account scope。
- 不存在合法 route、model 不受支持、connection 未启用时 Fail-Closed，不再回退到 default Adapter。
- 旧 `connection_id/target_connection_id` 仅作为 legacy hint；默认 mismatch 日志告警但不覆盖 server route。
- SessionResponse / MaterialResponse 不再公开内部 `connection_id`。

## Upload logging completion

修复 0.4.1 的四个日志盲区：

- JSON/multipart 解析失败现在记录 `material_request_parse_failed` + 最终 `material_upload_failed`。
- owner/purpose/route/policy/payload 校验失败现在可观察。
- readable URL / Dify 临时 URL 下载新增 `material_source_fetch_started/completed/failed`，包含 HTTP 状态、阶段、已接收字节与 traceback；日志 URL 自动去 query。
- `MaterialIngressError` 现在总会在 API 边界输出最终 `material_upload_failed`。

另外补充 fallback store、Material Registry、attempt audit 的阶段日志。

## Compatibility

- `/v1/jobs` 不变。
- 0.4.1 已有 Session/Request/Job 可继续按原冻结 connection 恢复；不会因 RouteCatalog 更新被重路由。
- 新 Dify 应删除 Relay 内部 connection 名，只传 channel/endpoint + provider/model。
- 迁移期推荐 `ROUTE_LEGACY_HINT_MODE=warn`；完成 Dify 瘦身后再改 `strict`。

## Database

无新增 migration。
