# Model Relay 8.8.1 HTTP Wire 日志与 Gemini 隐式缓存观测

本版基于 8.8.0 做诊断能力增强，不改变缓存决策、请求身份、Provider 路由或模型请求内容。

## HTTP Wire 日志配置迁移

HTTP Wire 日志改为按模型名 glob 规则自动启用，最终只需要两个环境变量：

```dotenv
RELAY_HTTP_WIRE_LOG_PROVIDERS="gemini-****"
RELAY_HTTP_WIRE_LOG_MAX_BODY_BYTES="262144"
```

`RELAY_HTTP_WIRE_LOG_PROVIDERS` 支持逗号分隔的多个模型名规则，使用大小写不敏感的 glob 匹配。空值表示关闭；`gemini-*` 和 `gemini-****` 都可以匹配 Gemini 模型。过滤对象是请求快照中的模型名，不是 Provider 名或 Channel 名。

以下旧变量不再需要，代码不再读取它们：

- `RELAY_HTTP_WIRE_LOG_ENABLED`：由模型规则是否匹配自动决定。
- `RELAY_HTTP_WIRE_LOG_REDACT_AUTH`：认证信息脱敏固定开启。
- `RELAY_HTTP_WIRE_LOG_DIRECTORY`：转储目录固定为 `/tmp/relay-http-wire`。

`RELAY_HTTP_WIRE_LOG_MAX_BODY_BYTES` 是日志内联 Body 的阈值：不超过阈值时写入结构化日志；超过阈值时完整转储到固定目录，并在日志中保留摘要、头尾片段及转储路径。URL 查询参数会被移除，敏感 Header 会脱敏；请求和响应 Body 本身仍可能包含 Prompt 或业务内容，应仅在受控环境开启。

## Gemini 原生缓存观测

当模型名匹配 `RELAY_HTTP_WIRE_LOG_PROVIDERS` 且 Provider 为 Gemini 时，`provider_call_completed` 结构化日志增加：

- `google_cached_content_token_count`：直接读取原始 Provider JSON 的 `usageMetadata.cachedContentTokenCount`；兼容 snake_case 变体。
- `google_cache_usage_field`：记录实际读取的原始字段路径。
- `provider_implicit_cache_status`：`provider_implicit_hit`、`provider_implicit_miss` 或 `unknown`。
- `provider_implicit_cache_reason`：解释判定依据或 unknown 原因。

判定规则：正整数为 `provider_implicit_hit`，明确的零为 `provider_implicit_miss`；字段缺失、格式非法或调用失败为 `unknown`。当 Relay 本轮发送了显式 Gemini CachedContent 资源时，即使该字段大于零，也标记为 `unknown`（原因 `explicit_cached_content_request`），避免把显式资源读取误报成 Google 原生隐式前缀命中。`provider_call_failed` 对匹配的 Gemini 模型记录 `unknown`。

这组观测不依赖 Relay 的 `requested_cache_mode`，因此 `requested_cache_mode=off` 时仍能观察 Google 原生隐式缓存是否返回了缓存 Token。

## 本版验证

- `compileall`：通过。
- 模型 glob：覆盖 `gemini-****`、多个模式、未匹配模型及空配置。
- 缓存观测：覆盖正数命中、零未命中、缺字段 unknown、snake_case 兼容、显式 CachedContent unknown、调用失败 unknown。
- Settings 模型：通过；旧开关、脱敏和目录字段已移除。
- 未执行真实 Google Provider 请求或生产部署认证；真实缓存命中仍以 Provider 返回的 usageMetadata 为准。

无需数据库迁移。API 与 Worker 应部署同一版本，并使用相同的模型日志规则配置。
