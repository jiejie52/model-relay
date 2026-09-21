# Model Relay 0.5.1 路由可用性修复实现

## 根因

0.5.0 的 built-in RouteCatalog 从 `ENABLED_CONNECTIONS` 生成。历史环境变量只包含 `aihubmix_default` 时，Gemini route 被从目录中直接删除，因此 `provider=gemini` 被误报为 `ROUTE_NOT_FOUND`。

## 0.5.1 修复

1. RouteCatalog 始终定义内置 provider/model -> connection 映射。
2. 默认 `CONNECTION_AVAILABILITY_MODE=all`；历史 `ENABLED_CONNECTIONS` 不再默认做 allowlist。
3. API/Worker 根据“connection 是否配置完整”自动注册现有 Adapter。
4. RouteResolver 将“无 route、model 不匹配、allowlist 禁止、凭据缺失、Adapter 缺失”拆成不同错误码。
5. route failure log 输出 server-side diagnosis，但业务响应不泄露内部 connection_id。

## 当前 Gemini 故障的预期变化

在环境仍有 `ENABLED_CONNECTIONS=aihubmix_default` 的情况下，只要：

```text
AIHUBMIX_API_KEY=...
AIHUBMIX_GEMINI_BASE_URL=...
```

Gemini 会正常解析到内部 Native route，不要求修改 `ENABLED_CONNECTIONS`。如果 Base URL/Key 缺失，则明确返回 `ROUTE_CONNECTION_NOT_CONFIGURED`，而不是 `ROUTE_NOT_FOUND`。

## 数据库

无 migration。
