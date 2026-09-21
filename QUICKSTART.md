# Model Relay 0.5.2 部署与迁移关键操作

本版本以 `0.5.1` 为基线，新增 Gemini 双材料传输策略，同时保留服务端 RouteResolver、默认全 Connection 可用、完整上传日志和 Raw Error。

1. **Route 仍由 Relay 服务端决定**：Dify 只传 `provider + model`，不负责 `connection_id / target_connection_id`。
2. **Gemini 按当前 Request 文件总量选材料传输**：`<=99 MiB` 使用 Supabase Signed External URL；`>99 MiB` 使用 Gemini Files API。两条路径都继续由 `GeminiNativeAdapter` 推理。
3. **Material 上传日志完整保留**：从 JSON/multipart 解析、参数/policy、Dify/HTTPS source fetch，到 Supabase/Files API 与 API 最终失败都有可串联日志。

## 1. 数据库

**0.5.1 -> 0.5.2 不需要执行新的 SQL。**

现有 0.4.1 数据库应已经执行过：

```text
sql/001_relay_schema.sql
sql/002_fusion_runtime.sql                  # 仍需旧 Fusion 时
sql/003_relay_v2_session_request_material.sql
sql/004_provider_native_file_ingress.sql
```

0.5.2 继续复用现有 `relay_materials / provider_material_bindings / material_fallback_objects` 与 Session/Request schema，因此无需新 migration。

## 2. Railway：Grok + Gemini Native

```text
DEPLOYMENT_ID=railway
EXECUTION_POOL=railway-default
WORKER_EXECUTION_POOLS=railway-default
CONNECTION_AVAILABILITY_MODE=all
# ENABLED_CONNECTIONS=... 在 all 模式下仅保留兼容，不参与路由限制

AIHUBMIX_API_KEY=...
AIHUBMIX_GEMINI_BASE_URL=<WF-NormalInference 中已验证的 Gemini Native Proxy base URL>
AIHUBMIX_GEMINI_CONNECTION_ID=aihubmix_gemini_native

ROUTE_REVISION=relay-route-catalog/2026-09-21.2
ROUTE_LEGACY_HINT_MODE=warn
```

默认内置 RouteCatalog：

```text
provider=gemini + model=gemini-* -> aihubmix_gemini_native
provider=grok   + model=grok-*   -> aihubmix_default
```

`connection_id` 是 Relay 内部事实，不再由 Dify 决定。

Gemini 推理 route 始终是 `GeminiNativeAdapter`，但输入文件传输分两条：

```text
当前 Request 全部文件总量 <= 99 MiB
  -> 原始文件写 Supabase Private Bucket
  -> Signed URL（默认 604800 秒）
  -> provider binding: gemini_external_url
  -> generateContent(fileData.fileUri=<signed https url>)

当前 Request 全部文件总量 > 99 MiB
  -> 不写 Supabase input-file object
  -> AIHubMix Gemini Native Proxy
  -> Gemini Files API /upload/v1beta/files
  -> upload, finalize / PROCESSING poll
  -> ACTIVE fileUri
  -> generateContent(fileData.fileUri=<Gemini fileUri>)
```

## 3. Aliyun SAE：Kimi Official

```text
DEPLOYMENT_ID=aliyun-sae
EXECUTION_POOL=aliyun-default
WORKER_EXECUTION_POOLS=aliyun-default
CONNECTION_AVAILABILITY_MODE=all
# ENABLED_CONNECTIONS=... 在 all 模式下不需要修改

MOONSHOT_API_KEY=...
MOONSHOT_BASE_URL=https://api.moonshot.cn/v1
MOONSHOT_CONNECTION_ID=moonshot_official

ROUTE_REVISION=relay-route-catalog/2026-09-21.2
ROUTE_LEGACY_HINT_MODE=warn
```

默认路由：

```text
provider=kimi + model=kimi-* -> moonshot_official
```

Kimi 文件仍按官方语义：

```text
文本/PDF/DOC/DOCX/TXT/MD -> purpose=file-extract -> GET /files/{id}/content
image/*                    -> purpose=image        -> ms://<file_id>
video/*                    -> purpose=video        -> ms://<file_id>
```

如果实际生产 Kimi 模型 ID 不以 `kimi-` 开头，请用 `ROUTE_CATALOG_JSON` 配置真实 model pattern，不要让 Dify 传内部 connection 来绕过路由。

## 4. 可选：自定义 RouteCatalog

路由目录只配置在 Relay，不下沉到 Dify。例如：

```text
ROUTE_CATALOG_JSON={"revision":"relay-route-catalog/2026-09-21.2","routes":[{"provider":"gemini","model_pattern":"gemini-*","connection_id":"aihubmix_gemini_native","priority":100,"deployment_id":"railway","requires_file_adapter":true},{"provider":"grok","model_pattern":"grok-*","connection_id":"aihubmix_default","priority":100,"deployment_id":"railway"}]}
```

启动时 Relay 会校验 route 结构一致性，但默认不会因为“另一个暂未配置的 Provider”阻止整个服务启动。真正选择某 route 时会分别判断：

- route 是否存在；
- model 是否匹配；
- 若显式启用 allowlist，connection 是否被允许；
- 所需服务端凭据/endpoint 是否配置；
- inference/file Adapter 是否注册且 Provider 一致。

错误会分别返回 `ROUTE_NOT_FOUND`、`ROUTE_MODEL_UNSUPPORTED`、`ROUTE_CONNECTION_DISABLED`、`ROUTE_CONNECTION_NOT_CONFIGURED`、`ROUTE_ADAPTER_NOT_REGISTERED` 或 `ROUTE_FILE_ADAPTER_NOT_REGISTERED`，不再把配置缺失混成 `ROUTE_NOT_FOUND`。

## 5. Material API：Gemini 99 MiB 双传输策略

`/v2/materials` 仍然一次创建一个 Material。为了严格按“本次模型请求全部文件总和”判断，调用方应把同一批文件的聚合信息随每个 Material 一起提交：

```text
request_file_total_bytes = 当前 Request 全部上传文件原始字节总和
request_file_count       = 当前 Request 文件数
material_batch_id        = 本次材料批次的稳定关联 ID（建议）
```

也可以通过 Headers 传：

```text
X-Relay-Request-File-Total-Bytes
X-Relay-Request-File-Count
X-Relay-Material-Batch-Id
```

JSON body 字段存在时优先于 Header。多个文件必须对同一批次发送相同的 `request_file_total_bytes / request_file_count / material_batch_id`。

### Gemini <= 99 MiB 示例

```json
{
  "tenant_id": "tenant-a",
  "conversation_hash": "conv-a",
  "provider": "gemini",
  "model": "gemini-3.1-flash-lite",
  "purpose": "inference_input",
  "filename": "report.pdf",
  "content_type": "application/pdf",
  "source_url": "https://...temporary...",
  "request_file_total_bytes": 73400320,
  "request_file_count": 2,
  "material_batch_id": "batch-20260921-001"
}
```

Relay 内部：

```text
RouteResolver -> Gemini internal route
source fetch + sha256
Supabase Storage object (private bucket)
Supabase Signed URL, TTL=SUPABASE_SIGNED_URL_TTL (default 604800)
provider binding representation=gemini_external_url
GeminiNativeAdapter -> fileData.fileUri=<signed url>
```

MaterialResponse 会显示 `fallback.stored=true` 和 `provider_binding.representation=gemini_external_url`，但不会把内部 connection 或 Signed URL 当成业务主键暴露；长期身份仍是 `material_id`。Signed URL 到期且 fallback object 仍存在时，BindingResolver 会重新签发 URL。

### Gemini > 99 MiB

```text
request_file_total_bytes > 103809024
 -> Gemini Files API
 -> provider binding representation=gemini_file_uri
 -> Supabase input-file object: 不创建
```

这一分支会忽略旧客户端针对输入原始字节设置的 `relay_backed/always`，避免违反“>99 MiB 不走 Supabase”的策略。Artifact/History/Raw Error 的 Supabase 使用不受影响。

### 聚合总量缺失时

如果 `request_file_total_bytes` 缺失，但 `request_file_count=1`，Relay 可用实际接收字节推断总量。若两者都不足以证明是单文件，Relay 会保守走 Gemini Files API，并记录 `gemini_request_file_total_missing`，防止多文件实际总量 >99 MiB 却错误走 External URL。

### Kimi / Grok / archive

Kimi 仍使用官方 Files API 语义；Grok 仍使用 Relay bridge/fallback 语义。`purpose=archive` 不触发 Provider Native Files API。

## 6. Session API：不再传 connection_id

创建 Session：

```json
{
  "tenant_id": "tenant-a",
  "conversation_hash": "conv-a",
  "provider": "gemini",
  "model": "gemini-3.1-flash-lite",
  "context_policy": "conversation",
  "material_ids": ["mat_xxx"]
}
```

Relay 在创建 Session 时解析一次 route，并冻结：

```text
provider
model
internal connection_id
account_scope_hash
route_revision
route_binding_hash
adapter versions
execution_pool
```

业务 SessionResponse 不返回内部 `connection_id`。

## 7. Session Request：只使用 Session 冻结路由

新合同请求示例：

```json
{
  "input": "请分析上传材料",
  "think_level": "medium",
  "execution": {"mode": "async"},
  "structured_output": {},
  "provider_payload": {},
  "metadata": {}
}
```

不要再传：

```text
provider
model
connection_id
```

这些执行事实从 Session 注入 Request snapshot。RouteCatalog 后续升级也不会改变既有 Session 的 route。

## 8. 旧 Dify 兼容窗口

Relay 建议先发布：

```text
ROUTE_LEGACY_HINT_MODE=warn
```

旧 DSL 即使仍传：

```text
connection_id=aihubmix_default
target_connection_id=aihubmix_default
```

它们也**只能作为 legacy hint**。例如客户端传：

```text
provider=gemini
model=gemini-3.1-flash-lite
connection_id=aihubmix_default
```

Relay 会记录：

```text
legacy_connection_hint_mismatch
```

但实际仍走服务端解析出的 `aihubmix_gemini_native`，不会被客户端 connection 误导。

Dify 完成瘦身后再改：

```text
ROUTE_LEGACY_HINT_MODE=strict
```

这时 legacy route hint 不一致直接 409。

**既有 Session 不会自动重路由。** 如果旧 Session 本身已经是错误 route（例如 Gemini Session 冻结成 `aihubmix_default`），应结束旧 Session 并新建 Session。

## 9. Supabase 对输入文件的新角色

对于原始输入文件 payload：

```text
Gemini Request 文件总量 <=99 MiB -> 主动写 Supabase，Signed URL 作为 Gemini External URL binding
Gemini Request 文件总量 >99 MiB  -> 不写 Supabase，使用 Gemini Files API
Kimi native 成功                    -> 默认不写 Supabase fallback
Grok/无 Native Files Store 的连接   -> 可作为 bridge
其他 Provider fallback/durability   -> 按原策略决定
```

以下数据仍继续使用现有 Relay Artifact Storage：

```text
request snapshot
raw response / raw error
session history
provider-derived extraction
Fusion artifacts
```

Storage fallback **只能改变文件字节存放方式，不能改变 Provider route / Wire / Adapter**。

## 10. 文件上传日志

建议生产配置：

```text
LOG_LEVEL=INFO
DEPENDENCY_HTTP_LOG_LEVEL=WARNING
UVICORN_ACCESS_LOG=false
```

因此 `httpx` 的 Supabase claim/heartbeat 轮询 200 日志默认不会刷屏。

每次 Material API 请求在得到 material_id 以前就生成 `ingress_id`。关键事件：

```text
material_upload_received
material_request_parse_failed
material_request_validation_failed
route_resolved / route_resolution_failed
material_ingress_started
material_policy_validation_failed
material_source_fetch_started
material_source_fetch_completed / material_source_fetch_failed
material_payload_validation_failed
material_registry_create_failed
material_route_bound
provider_file_binding_started
provider_file_binding_completed / provider_file_binding_failed
material_fallback_store_started
material_fallback_store_completed / material_fallback_store_failed
material_ingress_completed
material_upload_completed / material_upload_failed
```

典型 source fetch 失败会包含：

```text
ingress_id
provider/model
phase
source origin（仅 scheme + host/port，不记录 path/query）
duration_ms
upstream_http_status
bytes_received
failure_class
exception_type
traceback
```

Provider 文件上传失败还会包含：

```text
material_id
connection_id           # 仅内部日志
adapter_version
route_revision
generation
phase
upstream_http_status
upstream_request_id
stream_interrupted
bytes_received
traceback
```

普通日志不会打印 Authorization/API Key/Token，也不会打印模型输入正文。原始 Provider/source 错误仍通过 Error/detail/Raw Error 机制保真。

## 11. 路由日志

主要事件：

```text
route_catalog_validated
route_resolved
route_resolution_failed
legacy_connection_hint_mismatch
material_route_bound
session_route_frozen
legacy_request_route_hint_mismatch
request_route_snapshot_mismatch
route_binding_mismatch
```

生产排查 Gemini 时应看到一条链：

```text
route_resolved
  provider=gemini
  model=gemini-...
  connection_id=aihubmix_gemini_native
  adapter_version=gemini-native-aihubmix/1

material_route_bound
  file_adapter_version=...

provider_call_started
  connection_id=aihubmix_gemini_native
```

如果 route 无法解析，Material/Session 在调用 Provider 之前失败，禁止回退到 `aihubmix_default`。

## 12. 启动与健康检查

```bash
uvicorn app.api:app --host 0.0.0.0 --port 8000
python -m app.worker
```

健康检查：

```text
GET /health
version = 0.5.1-route-default-all
```

响应会提供 `route_revision` 与可用业务 Provider，不公开内部 connection 列表。

## 13. Dify 配套修改

Relay 0.5.1 可以先上线兼容旧 DSL；随后 Dify 应逐步删除：

```text
Parent relay_channel_resolver -> 不再生成 connection_id
Material Builder              -> 不再生成 target_connection_id，改传 provider/model/purpose
Session Prepare               -> 不再发送 connection_id
Session Request Builder       -> 不再发送 provider/model/connection_id
```

Dify 仍负责选择 Railway/SAE endpoint、provider、model；Adapter/connection/Wire 由 Relay 负责。

## 14. 最少验证

本地：

```bash
python -m compileall -q app
python -m unittest discover -s tests -v
```

预发布至少验证：

1. Railway + Gemini，不传 connection，日志 route_resolved 为 Gemini Native，推理使用 `fileData.fileUri`。
2. 旧客户端故意传 `gemini + aihubmix_default`，`warn` 模式应记录 mismatch，但实际仍进入 Gemini Native。
3. Gemini Material 不传 target_connection，仍自动选择 Gemini File Adapter。
4. Grok on Railway 自动解析 `aihubmix_default`，不误触 Gemini File API。
5. Kimi on SAE 自动解析 `moonshot_official`，File/Inference 都走官方 Kimi。
6. 同 Session 后续 Request 不传 route 字段，继续使用 Session 冻结 route。
7. source URL 返回 403/404/5xx、DNS 失败、超时、文件过大时，都能看到 `material_source_fetch_failed` + 最终 `material_upload_failed`。
8. JSON/multipart 非法、owner/policy/payload 非法时，都能看到对应 validation/parse 事件 + 最终失败事件。
9. route 未配置/模型不支持时 Material/Session Fail-Closed，不调用 Provider。
10. async Worker 恢复旧 Session 时不重新解析新 RouteCatalog。


## 0.5.1：默认 all 与未来 allowlist

当前建议保持：

```text
CONNECTION_AVAILABILITY_MODE=all
```

此时即使 Railway 仍存在历史变量：

```text
ENABLED_CONNECTIONS=aihubmix_default
```

也不会阻止 Gemini Native/Kimi route 被解析。只要对应 Key/Base URL 已配置，API/Worker 会自动注册 Adapter。

未来接入大量模型提供商后，如确实需要按部署做连接白名单，再改为：

```text
CONNECTION_AVAILABILITY_MODE=allowlist
ENABLED_CONNECTIONS=aihubmix_default,aihubmix_gemini_native,...
```
