# Model Relay 4.1.0 快速部署补充

> 4.1.0 纠正 4.0.0 的 Gemini Stateful Cache 创建方式：新 layout/4 按 **Files API -> `cachedContents.create` -> `generateContent(cachedContent=...)`** 执行，取消 Relay 的 `countTokens` 前置测量。

部署同一 4.1.0 镜像到 API/Worker，确认两端版本都是 `4.1.0`，并设置：

```text
MODEL_CONTROL_PLANE_REVISION=relay-model-control-plane/2026-10-02.1
GEMINI_CACHE_INLINE_FALLBACK_LIMIT_BYTES=73400320
```

要使用新链路必须**新建 Gemini Session**。4.0.0 layout/3 Session 保留原先冻结的 `countTokens` preflight，不会静默升级。

新 Session 的缓存主链：

```text
Material
  -> Gemini Files API first
  -> physical layout/4
  -> compatible CachedContent lookup
     -> miss: fenced cachedContents.create (no countTokens)
     -> hit/create success: CacheExecutionBinding(handle)
  -> seal model dispatch
  -> generateContent(cachedContent=<handle> + dynamic suffix)
```

4.0.0 的 Files 上传失败处理保持不变：总量 `<70 MiB` 时可把失败的 Session-stable 静态材料作为 `inlineData` 注入 Cache；总量 `>=70 MiB` 时确定性选择小文件子集（累计严格 `<70 MiB`）注入 Cache，其余较大文件通过 Supabase External URL 只进入 inference。

详细见 `DEPLOYMENT_4.1.0.md`。

> 下方 4.0.0 / 3.x 内容保留为历史迁移记录。

---

# Model Relay 4.0.0 快速部署补充

> 4.0.0 是新的 Gemini Material/Cache 处理需求：**Gemini Files API 永远优先**；Supabase 只在 Files 上传失败后作为 inference fallback；Cache 使用 Files URI 或按 70 MiB 规则选择的 `inlineData`，绝不把 Supabase External URL 放入 CachedContent。

部署同一 4.0.0 镜像到 API/Worker，确认两端 `/health` 都为 `4.0.0`。无新增 SQL migration，但 Gemini Stateful Cache 仍要求已有 `sql/005_relay_cache_control.sql` 与 `sql/006_gemini_stateful_cache.sql`。

新增环境变量：

```text
GEMINI_CACHE_INLINE_FALLBACK_LIMIT_BYTES=73400320
```

`GEMINI_FILES_THRESHOLD_BYTES` 仅为旧部署配置兼容，不再选择上传 transport。

Gemini 当前材料路径：

```text
Material ingress
  -> Gemini Files API first
     -> success: gemini_file_uri
        -> Session-stable: CachedContent(fileUri)
        -> request-only: Inference(fileUri)
     -> failure: Supabase fallback + Signed External URL (Inference only)
        -> exact Request total <70 MiB:
             all failed Session-stable files -> CachedContent(inlineData)
        -> exact Request total >=70 MiB:
             smallest failed Session-stable subset, cumulative raw bytes <70 MiB
                 -> CachedContent(inlineData)
             remaining larger failed files
                 -> Inference(fileData.fileUri=<Supabase Signed URL>)
```

使用缓存 handle 后，`generateContent` 只发送 `cachedContent` + 动态 history/input + inference-only 大文件；不会重复发送已经注入 CachedContent 的静态材料。详细见 `DEPLOYMENT_4.0.0.md`。

> 下方 3.x / 0.x 内容保留为历史迁移记录；涉及旧 `<=99 MiB -> Supabase / >99 MiB -> Files API` 的段落已被 4.0.0 顶部规则取代。

---

# Model Relay 3.2.0 快速部署补充

> 3.2.0 以 3.1.0 为基线，新增 Gemini Session Material 的版本化物理缓存投影。无新 SQL migration；数据库仍需已有 005 + 006。

部署同一 3.2.0 镜像到 API/Worker，确认两端 `/health` 都为 3.2.0。要使用 `GeminiPhysicalCachePlan` 新布局必须**新建 Gemini Session**；已有 Session 保持 3.1 wire layout。

建议先用 `requested_cache_mode=on` 验证：`countTokens` 只计 exact cached_prefix；达门槛后 CachedContent create 承载 Session Material，generateContent 仅发送 cache handle + exact uncached_suffix；不足门槛时发送完整 uncached context。创建结果无 handle 时只记录 `unknown` 并打印日志，不会自动重建。

完整说明见 `DEPLOYMENT_3.2.0.md`。

---

# Model Relay 3.1.0 快速部署补充

> 本次属于现有缓存处理方式增强，按 XX.YY.ZZ 规则从 3.0.0 升级到 **3.1.0**。完整迁移见 `DEPLOYMENT_3.1.0.md`。

先执行：

```text
sql/006_gemini_stateful_cache.sql
```

然后部署同一 3.1.0 镜像到 API/Worker。确认两端 `/health` 都为 3.1.0，并使用新的 `MODEL_CONTROL_PLANE_REVISION=relay-model-control-plane/2026-10-01.1`。已有 Session 继续冻结旧 RouteBinding；要启用 Gemini Stateful Cache 必须新建 Session。

调试建议先使用 `requested_cache_mode=on`。对于 `gemini-3.1-flash-lite` / `gemini-3.8-flash`：已提交 history/system instruction 达到显式缓存门槛时，执行应出现 `stateful_resource`；不足时仍会正常推理，但 effective mechanism 为 None。

---

# Model Relay 3.0.0 快速部署补充

> 本次为新增缓存机制需求，按 XX.YY.ZZ 规则从 2.0.0 升级到 **3.0.0**。完整迁移见 `DEPLOYMENT_3.0.0.md`。

数据库新增：

```text
sql/005_relay_cache_control.sql
```

部署顺序必须是：先应用 SQL 005，并发布可同时读取旧 Request 与 v3 Request 的 API/Worker，再允许创建 3.0 Session。新 Request 缓存意图只使用 `requested_cache_mode=off|auto|on`；Provider-native TTL/key/cache_control/cachedContent 不作为公共入口。

内置 3.0.0 只把已有 Grok cache-key 路径视作 `legacy_verified implicit_prefix`；Gemini/Claude/GPT 等新缓存能力在逐 Supply 真实认证前保持 candidate，不会因模型名或兼容协议被自动启用。

---

# Model Relay 2.0.0 快速部署补充

> 2.0.0 保留原有 Session/Request/Job 恢复语义，新增模型供应控制面。完整说明见 `DEPLOYMENT_2.0.0.md`；下方旧版本操作记录继续保留用于迁移追溯。

最小新增环境变量：

```text
MODEL_CONTROL_PLANE_REVISION=relay-model-control-plane/2026-09-27.1
AIHUBMIX_CHAT_CONNECTION_ID=aihubmix_chat_completions
AIHUBMIX_CLAUDE_CONNECTION_ID=aihubmix_claude_messages
AIHUBMIX_CLAUDE_BASE_URL=https://aihubmix.com
```

不设置 `MODEL_CONTROL_PLANE_JSON` 时使用内置 published snapshot。设置自定义快照时必须显式包含 `"status":"published"`；凭据通过 `credential_env` 引用，不写入 JSON。部署后同时检查 API 与 Worker `/health` 的 `control_plane_revision`、`control_plane_hash`、`control_plane_status` 和 `route_catalog_hash`。

本版本无新增 SQL migration。已有 Session 不因发布新优先级自动换渠道；需要采用新合同/新供应关系时创建新 Session。

---

# Model Relay 0.5.9 部署与迁移关键操作

> 当前增量：`kimi-k2.7-code*` 无论调用方选择何种 `think_level`，Relay 都统一映射为 Thinking ON，并由 Moonshot Adapter 发送 `thinking.type=enabled`。本增量无 SQL migration；部署后需新建 Kimi Session。

1. **Route 仍由 Relay 服务端决定**：Dify 只传 `provider + model`，不负责 `connection_id / target_connection_id`。
2. **Gemini 按当前 Request 文件总量选材料传输**：`<=99 MiB` 使用 Supabase Signed External URL；`>99 MiB` 使用 Gemini Files API。两条路径都继续由 `GeminiNativeAdapter` 推理。
3. **Material 上传日志完整保留**：从 JSON/multipart 解析、参数/policy、Dify/HTTPS source fetch，到 Supabase/Files API 与 API 最终失败都有可串联日志。

## 1. 数据库

**0.5.3 -> 0.5.4 不需要执行新的 SQL。**

现有 0.4.1 数据库应已经执行过：

```text
sql/001_relay_schema.sql
sql/002_fusion_runtime.sql                  # 仍需旧 Fusion 时
sql/003_relay_v2_session_request_material.sql
sql/004_provider_native_file_ingress.sql
```

0.5.4 只修改 Signed URL 解析逻辑，继续复用现有 `relay_materials.actual_size / provider_material_bindings / material_fallback_objects` 与 Session/Request binding snapshot，因此无需新 migration。

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

0.5.4 对 Supabase `signedURL` 的 normalization 规则：

```text
https://...                   -> 原样使用
/storage/v1/object/...       -> SUPABASE_URL + path
storage/v1/object/...        -> SUPABASE_URL + / + path
/object/sign/...             -> SUPABASE_URL + /storage/v1 + path
object/sign/...              -> SUPABASE_URL + /storage/v1/ + path
```

这与 `WF-NormalInference_20260910-NoComments.yml` 的关键做法一致：相对 `signedURL` 必须基于 `storage_base = root + /storage/v1` 解析，而不能直接拼到项目 root。

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

## 5. Material API：Gemini 99 MiB Relay 权威计算策略

`/v2/materials` 仍然一次创建一个 Material，但调用方**不再需要计算总字节数**。Relay 会读取实际文件 bytes、写入 `actual_size`，并在真正的 Session Request 冻结时对最终材料集合自行求和。

0.5.2 的以下字段仍可提交，但只是兼容/诊断 hint，不能控制 transport：

```text
request_file_total_bytes
request_file_count
material_batch_id
X-Relay-Request-File-Total-Bytes
X-Relay-Request-File-Count
X-Relay-Material-Batch-Id
```

如果 hint 与 Relay 实际测量不一致，会打印 `gemini_client_size_hint_ignored`。

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
  "source_url": "https://...temporary..."
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
Relay 在 Request 冻结时 sum(actual_size) > 103809024
 -> Gemini Files API
 -> provider binding representation=gemini_file_uri
 -> 删除为 External URL bridge 暂存的 input-file Supabase 副本
```

调用方是否提交 `request_file_total_bytes` 不影响判断。Relay 对最终 Session + Request 材料集合重新求和，并在 Provider dispatch 前冻结正确 binding generation。Artifact/History/Raw Error 的 Supabase 使用不受影响。

### 不再要求调用方聚合总量

缺少 `request_file_total_bytes/request_file_count/material_batch_id` 不再触发保守 Files API。Relay 以实际接收的 bytes 作为 Material 权威 size，并在 Request 阶段自行求和。旧字段仅用于日志诊断；不一致时记录 `gemini_client_size_hint_ignored`。

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
  "options": {
    "temperature": 0.08,
    "max_output_tokens": 8000
  },
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

`options` 是 Provider-Neutral 高级参数，不是 Provider Wire。当前支持：

```text
temperature
top_p
max_output_tokens
```

Relay 在 Request 受理时做 Capability 校验并冻结 `effective_options`；上述是 canonical option namespace，具体 provider/model 可用子集由 Relay capability profile 决定。当前实现中 `gemini-3.5-*` / `gemini-3.6-*` 支持 `temperature` 与 `max_output_tokens`，其中 `temperature` 由 Gemini Native Adapter 投影到 `generationConfig.temperature`；`top_p` 对这两个模型族暂保持 Fail-Closed。Gemini/Grok/Kimi 的具体字段名只在对应 Provider Adapter 内生成。`provider_payload` 仅作为 v2.1 迁移字段保留，未知 legacy wire 字段会被拒绝。

Kimi K3 (`kimi-k3*`) 的 `think_level` 支持 `auto/low/high/max`。其中 `low/high/max` 由 MoonshotChatAdapter 投影为顶层 `reasoning_effort`，`auto` 不发送该字段；其他 `kimi-*` 仍保持 `auto`-only，避免对没有可调 effort 合同的模型伪装支持。

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


## Kimi K2.7 Code 与 Dify 思考深度兼容

`kimi-k2.7-code*` 接受 `think_level=auto/low/high/max`。K2.7 Code 为固定 Thinking ON；Relay 对 `low/high/max` 做受理兼容，并将 effective think level 规范化为 `auto`，因此不会在受理阶段返回 `THINK_LEVEL_UNSUPPORTED`，也不会向上游发送未经官方文档确认的 `reasoning_effort`。如需实际可调的 low/high/max effort，请使用支持该原生能力的 Kimi 模型。
