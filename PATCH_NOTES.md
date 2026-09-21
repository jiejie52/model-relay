# Model Relay 0.4.1 Patch Notes

基线：`model-relay-v2_0.4.0`
范围：生产日志与可观测性优化，**无 SQL migration**。

## 默认降噪

- `httpx/httpcore/hpack` 默认 `WARNING`：不再出现 Worker 轮询 Supabase `claim_relay_job_v2` 的每次 `200 OK`。
- `uvicorn.access` 默认关闭：health/status/result 的正常轮询不再占据主要日志。
- 可用 `DEPENDENCY_HTTP_LOG_LEVEL=INFO` 临时恢复依赖 HTTP access log。

## 请求生命周期日志

- `request_accepted`：Request 已持久化；带 request/session/job、sync/async、provider/connection/model/pool。
- `job_claimed`：Worker 领取异步 Job；带 worker、lease_epoch、execution_pool。
- `request_execution_started`、`material_bindings_frozen`、`provider_call_started`、`provider_call_completed`、`request_execution_committed`。
- Provider 成功记录耗时、HTTP 状态、Provider request/response id、响应字节数。

## 错误与流中断

- `provider_call_failed` / `provider_file_binding_failed` 输出 request/material 关联信息、failure_class、HTTP 状态、Provider request id、phase 和 traceback。
- `upstream_stream_interrupted` 在已经拿到响应头但读取 body 中途断开时记录 `bytes_received/http_status/upstream_host/upstream_path`；URL query 不记录。
- `request_executor_timeout` / `request_executor_failed` 记录最终执行层异常和 traceback。
- `failure_class` 区分 client、upstream_rejected、upstream、upstream_timeout、upstream_transport、relay_validation、relay_configuration、relay。

## 安全边界

- 结构化日志不打印请求/模型正文、不打印 Raw Error body。
- Authorization、Token、API Key、Secret 字段统一脱敏。
- 原始 Provider Error 的完整交付机制保持 0.3.1+ 行为不变。

## 部署

0.4.0 数据库无需变化。替换代码并同时重启 API 与 Worker 即可。

