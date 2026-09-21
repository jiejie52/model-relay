# Model Relay Changelog

## 0.5.4 Supabase Signed URL Normalization

- Fixed Supabase Storage `signedURL` normalization for relative `/object/sign/...` responses.
- Relative Storage paths are now resolved against `<SUPABASE_URL>/storage/v1`, matching the proven WF-NormalInference Supabase bridge behavior.
- Added compatibility for absolute URLs and already Storage-rooted `/storage/v1/...` responses without duplicating the prefix.
- Gemini Relay-authoritative `<=99 MiB` / `>99 MiB` transport decision is unchanged.
- Added 5 regression tests for Signed URL response shapes; full suite now passes 68 tests.
- No database migration.

## 0.5.3 Relay-Authoritative Gemini Size

- Gemini file-size authority moved from caller aggregate hints to Relay-measured bytes.
- `/v2/materials` uses actual received bytes; missing `request_file_total_bytes` no longer forces Gemini Files API.
- Before provider dispatch Relay sums the exact Request material `actual_size` values and makes the final `<=99 MiB` / `>99 MiB` transport decision.
- Multi-file batches that cross the threshold are promoted from External URL bindings to Gemini Files API before inference; the input-file Supabase fallback copies are removed after provider binding succeeds.
- Caller total/count/batch fields remain compatibility diagnostics only and cannot override Relay routing.
- Legacy 0.5.2 small materials that only have a Files API binding and no fallback bytes fail closed with `MATERIAL_REUPLOAD_REQUIRED` when a <=99 MiB request requires External URL.
- Added authoritative-size and request-level promotion/cleanup structured logs.
- No database migration.

## 0.5.2 Gemini Dual Transport

- Gemini inference route remains `GeminiNativeAdapter`; only the material transport changes.
- Current Request aggregate file bytes `<= 99 MiB (103809024)` -> Supabase input object + Signed External URL -> Gemini `fileData.fileUri`.
- Current Request aggregate file bytes `> 99 MiB` -> AIHubMix Gemini Native Proxy -> Gemini Files API -> `fileUri`; input bytes are not written to Supabase.
- `/v2/materials` adds `request_file_total_bytes`, `request_file_count`, `material_batch_id` plus equivalent `X-Relay-*` headers for one-material-at-a-time ingress.
- Missing aggregate information falls back conservatively to Files API unless Relay can prove the request contains exactly one file.
- Signed URL lifetime reuses `SUPABASE_SIGNED_URL_TTL` (default 604800 seconds) and expired External URL bindings are re-signed from the retained Relay object without Files API re-upload.
- Added Gemini transport-selection logs and regression coverage for threshold boundary, >99 no-Supabase behavior, missing aggregate and URL re-sign.
- No database migration.

## 0.5.1 Default-All Connections + Clear Route Diagnostics

- 默认 connection policy 改为 `all`，旧 `ENABLED_CONNECTIONS` 不再误伤 Gemini/Kimi route。
- RouteCatalog 与连接可用性解耦：route 定义独立于 credentials/Adapter/allowlist。
- Gemini route 在服务端 Key/Base URL 配置完整时自动注册 Native inference/file Adapter。
- route 错误细分为 NOT_FOUND、MODEL_UNSUPPORTED、CONNECTION_DISABLED、CONNECTION_NOT_CONFIGURED、ADAPTER_NOT_REGISTERED、FILE_ADAPTER_NOT_REGISTERED。
- `route_resolution_failed` 日志补充 configuration reason、model patterns、registered adapters 与 connection policy。
- 未来需要白名单时显式启用 `CONNECTION_AVAILABILITY_MODE=allowlist`。
- 无数据库 migration。

## 0.5.0 Route Decoupling + Upload Observability

- 新增 server-side RouteCatalog/RouteResolver；public contract 2.1 由 provider/model 决定内部 route。
- Gemini/Grok/Kimi Adapter 自动选择，Material File Adapter 与 Inference Adapter 共享冻结 RouteBinding。
- legacy connection hint 不再拥有路由权；warn 模式忽略 mismatch，strict 模式 Fail-Closed。
- Session/Material 业务响应隐藏内部 connection_id；Session metadata 保存 route revision/hash。
- 补齐 JSON/multipart、参数/policy、source fetch、fallback、API final failure 全链路结构化日志。
- readable URL 日志仅保留 source origin，不记录 path/query；source fetch HTTP/transport/timeout/size/policy 失败可区分。
- 新增 route/material route mismatch 断言，恢复时禁止静默切换 Adapter 或 account scope。
- 无数据库 migration；继续使用 004 后的表结构。
- 单元测试扩展到 RouteResolver、legacy hint、Fail-Closed、source fetch 与 API final-failure logging。

# Relay v2 改造摘要

## 0.4.1 Production Observability

- 默认关闭 `httpx/httpcore` INFO，去除 Supabase `claim_relay_job_v2`、Lease/Heartbeat 等高频成功轮询噪音。
- 默认关闭 Uvicorn access log，由 Relay 业务事件替代普通 access log。
- 新增单行结构化 JSON lifecycle logs：Request 接收、Job claim、执行开始、Material binding、Provider dispatch、原子提交。
- Provider 成功日志增加 `duration_ms/http_status/upstream_request_id/provider_response_id/response_bytes`。
- Provider/Files API 失败日志增加 `failure_class/upstream_http_status/phase/exception_type` 并输出 traceback。
- response body 读取中断新增 `upstream_stream_interrupted`，记录已接收字节与上游 HTTP 上下文；不记录 URL query。
- 日志字段统一做敏感键脱敏；Token/API Key/Authorization 不进入业务日志。
- 新增 `DEPENDENCY_HTTP_LOG_LEVEL`、`UVICORN_ACCESS_LOG` 配置。
- 无数据库 migration；0.4.0 -> 0.4.1 仅需替换代码并重新部署 API/Worker。

## 0.4.0 Provider-native File Ingress

- Gemini 输入文件默认通过 AIHubMix Gemini Native Proxy 直传 Gemini Files API，不先持久化 Supabase input-file object。
- 新增 Gemini native generateContent Adapter，模型请求直接使用冻结的 `fileUri` binding。
- Kimi 输入文件默认直连官方 Files API：文本 `file-extract + /content`，图片/视频使用 `ms://<file_id>`。
- Supabase 对输入原始文件收窄为 fallback/bridge/durability；Request/History/Raw Error/Provider-derived artifact 的现有 Supabase 用法保留。
- `material_id` 与 Provider resource 解耦，新增 account scope、binding generation、binding attempts、fallback objects、Request binding snapshot。
- Provider file binding 失效且没有 fallback/source 时 Fail-Closed 为 `MATERIAL_REUPLOAD_REQUIRED`。
- Files API 原始 Error 完整保真，不截断、不归一化。
- 新增 `sql/004_provider_native_file_ingress.sql`。
- 单元测试新增 provider-native/fallback/account-scope/Kimi wire 语义覆盖。

## 0.3.1 Error Passthrough 修复

- 修复 v2 Error Envelope 只返回 `body_size/body_sha256/body_object_id`、未返回原始 Provider body 的问题。
- 文本/JSON 错误新增完整 `body_text`；所有错误新增精确 `body_base64`。
- `body_encoding` 改为按 Content-Type charset 判定；只有真实二进制或严格解码失败时才为 `binary`。
- 保留 `/error/raw` 与 Storage 原始字节归档；旧 0.3.0 Error 可在查询时从 `body_object_id` 自动回填正文。
- Provider/Supabase HTTP 错误的 `message` 同步使用完整原始文本正文，兼容只读取 `error_message` 的 Dify 调用方。
- 无数据库结构变更。


## 新增

- `/v2/materials`：Material Ingress，临时源立即读入共享 Object Storage，记录 SHA-256/size/storage_id。
- `/v2/sessions`：统一 Session，固定 provider/connection/model/context policy/execution pool。
- `/v2/sessions/{id}/requests`：统一 Request；支持 `sync`/`async`，只有 async 创建 Job。
- `relay_requests`：新的调用事实表；`relay_jobs` 降为异步调度表。
- `relay_objects`：所有 v2 大对象的存储定位事实，显式保存 `storage_id`。
- `relay_materials` 与 `provider_material_bindings`：稳定材料身份 + 可重建 Provider binding。
- 官方 Kimi/Moonshot Chat Adapter，文件提取、Base64 图片、Provider file binding 重建。
- 原始 Error 通道：完整 body + hash + HTTP 元数据，新增 `/error/raw`。
- Worker `execution_pool` 过滤、`lease_epoch` fencing、`result_stored` 后恢复提交、Provider dispatch 后失租转 `indeterminate`。
- sync Request deadline；超时/进程丢失不自动转 async 重放。
- ObjectStorage/StorageRegistry 接口，为 R2/OSS 后端预留。
- Worker SIGTERM drain。

## 保留

- 原 Postgres durable Job queue、`FOR UPDATE SKIP LOCKED`、Lease、Heartbeat。
- `/v1/jobs`、旧 job_id 查询/取消与旧 Fusion 兼容执行。
- Grok `response.output`/encrypted reasoning 外部历史。
- Structured Output provider projection + canonical JSON Schema 校验。
- Supabase Postgres/Storage 首期继续作为 Railway/SAE 共享控制面和对象存储。

## 明确改变

- 新链路不再以 Job 作为所有调用的身份；Request 才是事实权威。
- 新链路不再把 Dify/Fusion stage 作为 Relay Core 的执行分支。
- Provider Error 不再映射为 `UPSTREAM_BAD_REQUEST/UPSTREAM_SERVER_ERROR`，不做摘要/截断。
- Signed URL / Provider file id 不再作为材料长期事实。
