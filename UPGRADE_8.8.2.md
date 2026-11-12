# Model Relay 8.8.2 HTTP Wire 日志配置进一步精简

本版以 Model Relay 8.8.1 完整源码包为基线，只调整 HTTP Wire 日志 Body 阈值的配置方式，不改变 8.8.1 已有的日志筛选、脱敏、日志目录或 Gemini 原生隐式缓存观测行为。

## 最终环境变量

现在只需要配置模型名匹配规则：

```dotenv
RELAY_HTTP_WIRE_LOG_PROVIDERS="gemini-****"
```

`RELAY_HTTP_WIRE_LOG_PROVIDERS` 仍支持逗号分隔的多个模型名 glob 规则，匹配不区分大小写；空值表示关闭 HTTP Wire 日志。匹配对象是请求上下文中的模型名，而不是 Provider 或 Channel 名。

## Body 大小阈值

`RELAY_HTTP_WIRE_LOG_MAX_BODY_BYTES` 已从 Settings 和 `.env.example` 中移除，不再需要配置。

HTTP Wire 日志的内联 Body 上限固定为 **262144 bytes（256 KiB）**：

- Body 不超过 262144 bytes：以内联十六进制内容写入结构化日志。
- Body 超过 262144 bytes：完整转储到固定目录 `/tmp/relay-http-wire`，结构化日志保留摘要、头尾片段和转储路径。

如果部署环境的 `.env` 中仍保留旧的 `RELAY_HTTP_WIRE_LOG_MAX_BODY_BYTES`，该变量不再被代码读取；建议同步删除，避免造成仍可调节阈值的误解。

## 保持不变的 8.8.1 行为

- 模型名 glob 过滤与空配置关闭行为。
- Header/Auth 敏感信息始终脱敏。
- Wire dump 目录固定为 `/tmp/relay-http-wire`。
- Gemini 原始响应 `usageMetadata.cachedContentTokenCount` 观测。
- `provider_implicit_hit`、`provider_implicit_miss`、`unknown` 三态判定。
- 显式 CachedContent 请求防误判逻辑。
- 不改变缓存决策、请求前缀、Session 历史、Provider 路由或模型请求内容。

无需数据库迁移。API 与 Worker 应部署同一版本。Body 可能包含 Prompt 或业务内容，仍建议只在受控诊断环境开启 HTTP Wire 日志。

## 验证边界

已执行源码编译、配置引用检查和模型 glob 规则检查；未执行真实 Google Provider 请求或生产部署验证。
