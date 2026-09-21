# Model Relay 0.5.3 Patch Notes

## Relay-authoritative Gemini size

0.5.3 removes the 0.5.2 dependency on caller-provided aggregate bytes. Relay measures each uploaded payload itself, persists `actual_size`, and recomputes the exact Request material total immediately before binding freeze/provider dispatch.

- `<= 99 MiB`: Supabase Private Bucket + 7-day-renewable Signed URL + `gemini_external_url`.
- `> 99 MiB`: Gemini Files API + `gemini_file_uri`.
- `request_file_total_bytes/request_file_count/material_batch_id` are compatibility diagnostics only.
- Missing aggregate hints no longer select Files API.
- Multi-file Request totals are re-evaluated by Relay; if the sum crosses 99 MiB, External URL materials are promoted to Files API and their input-file Supabase copies are removed before inference.
- Existing frozen Request snapshots remain immutable on retry.
- No SQL migration.

See `GEMINI_RELAY_AUTHORITATIVE_SIZE_0.5.3_IMPLEMENTATION.md`.

---

# Model Relay 0.5.2 Patch Notes

## Gemini dual material transport

Gemini 的 Provider route 不变，始终由服务端 `RouteResolver` 解析到 Gemini Native route；本版只改变输入文件的 transport binding：

- 当前 Request 上传文件原始字节总和 `<= 99 MiB (103809024 bytes)`：写入 Supabase Private Bucket，生成 Signed URL，binding `representation=gemini_external_url`，由 `GeminiNativeAdapter` 作为 `fileData.fileUri` 使用。
- 当前 Request 上传文件原始字节总和 `> 99 MiB`：不创建 Supabase input-file object，继续走 AIHubMix Gemini Native Proxy -> Gemini Files API，binding `representation=gemini_file_uri`。
- Signed URL TTL 使用现有 `SUPABASE_SIGNED_URL_TTL`，默认 604800 秒（7 天），并限制最大 7 天。
- Signed URL 过期时，若 Relay fallback object 仍存在，只重新签 URL，不重新上传 Gemini Files API。

## Aggregate contract

`POST /v2/materials` 一次只接收一个 Material，因此新增以下批次提示来准确执行“当前 Request 文件总量”规则：

- `request_file_total_bytes`
- `request_file_count`
- `material_batch_id`

也支持：

- `X-Relay-Request-File-Total-Bytes`
- `X-Relay-Request-File-Count`
- `X-Relay-Material-Batch-Id`

JSON/body 字段优先于 Header。聚合总量缺失且不能确定为单文件时，Relay 保守使用 Files API。

## Supabase behavior

小文件路径复用现有 `FallbackObjectStorage -> SupabaseObjectStorage`：保留 Content-Type、SHA-256、size、storage_id、bucket/object_key；Supabase REST upload 使用 `x-upsert=true`，Signed URL 通过 Storage sign endpoint 创建。Relay 不复制 Dify Workflow 中针对临时 URL 的不安全 TLS 兼容逻辑，source fetch 继续遵守 Relay SSRF/TLS policy。

## Observability

新增/强化：

- `gemini_material_transport_selected`
- `gemini_request_file_total_missing`
- `gemini_external_url_sign_started/completed`
- `gemini_external_url_binding_failed`
- `gemini_files_supabase_policy_suppressed`

既有 source fetch / storage / Provider / API final-failure 日志继续保留。

## Database

0.5.1 -> 0.5.2 无新增 SQL migration。
