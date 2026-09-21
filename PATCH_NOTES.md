# Model Relay 0.5.1 Patch Notes

## Default-all connection availability

- 默认 `CONNECTION_AVAILABILITY_MODE=all`。
- 旧 `ENABLED_CONNECTIONS` 在默认模式下不再限制 Route/Adapter；只有显式切换 `allowlist` 才生效。
- Built-in RouteCatalog 始终定义 Gemini/Grok/Kimi route，不再因为旧 allowlist 变量把 route 从目录中删除。
- Adapter 注册改为“服务端配置完整 + connection policy 允许”；默认 all 模式下只要 Key/Base URL 齐全就自动注册。

## Clear route diagnostics

- `ROUTE_NOT_FOUND` 只表示 Provider 在当前 RouteCatalog 中没有 route 定义。
- Provider 有 route 但 model 不匹配：`ROUTE_MODEL_UNSUPPORTED`。
- 显式 allowlist 禁止：`ROUTE_CONNECTION_DISABLED`。
- route 已解析但 Key/Base URL 缺失：`ROUTE_CONNECTION_NOT_CONFIGURED`。
- inference Adapter 未注册：`ROUTE_ADAPTER_NOT_REGISTERED`。
- Native File Adapter 未注册：`ROUTE_FILE_ADAPTER_NOT_REGISTERED`。
- `route_resolution_failed` 增加 connection policy/configuration、model patterns、registered adapters 等诊断字段。

## Compatibility / Database

- 0.5.0 public contract 2.1 不变；Dify 仍只需要 `provider/model`。
- legacy `connection_id/target_connection_id` hint 行为不变。
- 无新增 SQL migration。
