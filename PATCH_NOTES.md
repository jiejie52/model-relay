# Model Relay 0.3.1 Patch Notes

本版本基于 `model-relay-v2_0.3.0`，修复 v2 Session/Request Error Envelope 丢失 Provider 原始错误正文的问题。

主要修复：

- `RawErrorRecorder` 不再把所有错误硬编码为 `body_encoding="binary"`。
- 对 `application/json; charset=utf-8`、`text/*`、`+json`、XML 等文本错误，按声明字符集严格解码并返回完整 `body_text`。
- Error Envelope 新增 `body_base64`，保存并返回未经截断的精确原始响应字节；`body_size`/`body_sha256` 与该原始字节一致。
- `RawErrorMeta` 增加 `body_text` / `body_base64`，防止 Pydantic 在 API 序列化时过滤原始正文。
- 对 Provider/Supabase HTTP 错误，`error.message` 也改为完整原始文本正文，避免 Dify 只读取 `error_message` 时仍看到通用占位文案。
- 对 0.3.0 已失败且已归档 `body_object_id` 的 Request，API 会在查询/幂等重放时从 Storage 回填原文，无需改幂等键或补数据库数据。
- `/error/raw` 归档读取接口继续保留，Storage 中的原始错误对象及 `body_object_id` 机制不变。
- 新增 360 字节 JSON Provider 错误回归测试，验证 Recorder -> Pydantic -> Request Envelope 全链路不丢正文。
- 新增二进制错误测试，确认不可安全解码时仍标记为 `binary`，并通过 Base64 完整交付。

升级说明：

- **无需执行新的 SQL migration**；`relay_requests.error` 已是 JSONB，可直接保存新增字段。
- 直接用 0.3.1 镜像/源码替换 0.3.0 并重启 API 与 Worker。
- `/health` 版本应显示 `0.3.1-session-request-material-error-passthrough`。
