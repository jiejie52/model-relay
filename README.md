# Model Relay 0.5.7 - Kimi Reasoning Effort

本版本以 `model-relay-v2 0.5.5` 为基线，修复 Gemini 3.5/3.6 capability profile 误拒绝 canonical `options.temperature` 的问题。业务 Workflow 继续只表达 Provider-Neutral options；Gemini Native Adapter 统一负责把 temperature 投影到 `generationConfig.temperature`。无需修改 Parent / Analyze / Finalize DSL。


## 0.5.7 关键变化

- Kimi K3 (`kimi-k3*`) 的 canonical `think_level` 新增 `low` / `high` / `max`；`auto` 继续保留。
- MoonshotChatAdapter 将 `low/high/max` 1:1 投影为官方 Chat Completions 顶层 `reasoning_effort`；`auto` 不发送该字段，使用上游模型默认档位。
- `reasoning_effort` 与 `thinking` 被纳入 Adapter 保护字段，调用方不能通过 legacy `provider_payload` 绕过 Relay capability。
- 其他 `kimi-*` 仍保持 `auto`-only；K2.7 Code 不伪装支持可调 effort。
- `CAPABILITY_PROFILE_REVISION` 升级为 `relay-model-options/2026-09-23.2`。因为能力语义发生变化，升级后应为 Kimi 新建 Session；旧 Session 提交新 Request 会按设计返回 `CAPABILITY_PROFILE_CHANGED_RECREATE_SESSION`。
- Request schema、Session/Request/Job、Checkpoint/Resume、Material、Route、Structured Output 均不变；无数据库 migration。

## 0.5.6 关键变化

- `gemini-3.5-*` 与 `gemini-3.6-*` 现在接受 canonical `options.temperature`。
- Gemini Native wire 继续由 Adapter 构造：`options.temperature -> generationConfig.temperature`；不会恢复 caller `provider_payload` 或顶层 `temperature`。
- `max_output_tokens` 行为不变；`top_p` 在 3.5/3.6 仍保持 Fail-Closed，等待单独验证后再启用。
- `CAPABILITY_PROFILE_REVISION` 保持 `relay-model-options/2026-09-23.1`：0.5.6 修复的是 0.5.5 已声明 canonical temperature contract 的实现偏差，避免现有 Session 因纯 Bugfix 被强制重建。
- Request schema/hash、Session/Request/Job、Checkpoint/Resume、Material、Route、Structured Output 均不变。
- 无数据库 migration。

详细实现见 `GEMINI_TEMPERATURE_CAPABILITY_0.5.6_IMPLEMENTATION.md`。

## 0.5.5 关键变化

本版本以 `model-relay-v2 0.5.4` 为基线，把 v2 Request 的高级模型参数从 `provider_payload` 收口为 Provider-Neutral `options`，并新增 Relay Capability 校验与 Adapter 原生 Wire 投影。核心目标是：业务调用方表达参数意图，Relay 冻结生效参数，Provider Adapter 独占 Gemini / Grok / Kimi 原生字段映射。

- `/v2/sessions/{session_id}/requests` 新增 `options`，当前 canonical 支持 `temperature`、`top_p`、`max_output_tokens`。
- Capability 同时校验 `think_level`：Gemini 支持 `auto/low/medium/high` 并映射到 Native `thinkingConfig.thinkingLevel`；Grok 4.6 额外支持 `xhigh`；当前 Kimi Adapter 未实现 reasoning-effort wire，因此非 `auto` Fail-Closed，避免“看似生效、实际忽略”。
- 新增 `app/model_options.py` 与 `CAPABILITY_PROFILE_REVISION`；Request 受理阶段先校验 provider/model/option，再冻结 `options + effective_options + capability_revision`。
- v2.2 Adapter 禁止把 caller dictionary 任意 merge 到 Provider JSON；Gemini 将 `temperature/top_p/max_output_tokens` 映射到 `generationConfig.temperature/topP/maxOutputTokens`，Grok Responses 映射为原生 Responses 字段，Kimi 将 `max_output_tokens` 映射为 `max_tokens`。
- `provider_payload` 仅保留 v2.1 迁移桥：只接受可确定映射的 legacy alias；未知 provider wire 字段 Fail-Closed。
- 修复已持久化 v2.1 Gemini Request 的恢复语义：legacy `provider_payload.temperature` 会迁移到 `generationConfig.temperature`，不再发送非法顶层 `temperature`。
- Session RouteBinding 冻结 `capability_revision`；新 Request 若发现 Session capability 与当前版本不一致，返回 `CAPABILITY_PROFILE_CHANGED_RECREATE_SESSION`，避免参数语义静默漂移。
- v2.2 request hash 改为包含 canonical `options/effective_options/capability_revision`；旧 v2.1 request identity 继续兼容。
- `/health` 新增 `capability_revision`。
- 无新增 SQL migration。
- 回归结果：`compileall PASS`、API import PASS、Worker import PASS、`pytest 82 passed`。

详细实现见 `CANONICAL_MODEL_OPTIONS_0.5.5_IMPLEMENTATION.md`。

## 0.5.4 关键变化

- 修复 `/object/sign/...` 被错误拼成 `<SUPABASE_URL>/object/sign/...` 的问题；正确结果为 `<SUPABASE_URL>/storage/v1/object/sign/...`。
- 参考 `WF-NormalInference_20260910-NoComments.yml` 已验证逻辑：相对签名路径以 Storage API base `<root>/storage/v1` 为基准解析。
- 同时兼容绝对 URL、`/storage/v1/...`、`storage/v1/...`、`/object/sign/...` 与 `object/sign/...`，避免重复或遗漏 `/storage/v1`。
- 99 MiB 阈值、Supabase Private Bucket、7 天 Signed URL、Gemini Files API 大文件路径均不变。
- 无新增 SQL migration。

详细实现见 `SUPABASE_SIGNED_URL_NORMALIZATION_0.5.4_IMPLEMENTATION.md`。

## 0.5.3 关键变化

- Relay 下载/接收材料后直接以实际 bytes 写入 `actual_size`；SHA-256 与 size 都由 Relay 计算。
- 缺少 `request_file_total_bytes` 时不再保守走 Gemini Files API。单文件 547024 bytes 这类场景会直接判定为 `<=99 MiB`，进入 Supabase Signed External URL。
- 真正调用模型前，Relay 对 Session + Request 的最终 `material_ids` 重新读取 `actual_size` 并求和。
- Request 总量 `<=99 MiB`：Supabase Private Bucket -> Signed URL -> `gemini_external_url` -> Gemini `fileData.fileUri`。
- Request 总量 `>99 MiB`：Gemini Files API -> `gemini_file_uri`。若多个小文件此前已有 External URL bridge，Relay 会在 dispatch 前升格到 Files API，并删除输入文件 Supabase 副本。
- 0.5.2 的 `request_file_total_bytes/request_file_count/material_batch_id` 与 `X-Relay-*` headers 继续兼容，但只用于诊断，不能覆盖 Relay 自己的实际 size。
- 无新增 SQL migration。

详细实现见 `GEMINI_RELAY_AUTHORITATIVE_SIZE_0.5.3_IMPLEMENTATION.md`。

## 关键边界

- `/v2/materials` 新合同使用 `provider/model/purpose`，不再要求 `target_connection_id`。
- `/v2/sessions` 新合同使用 `provider/model`，不再要求 `connection_id`。
- `/v2/sessions/{session_id}/requests` 从 Session 读取已冻结 route；Request 不再决定 provider/model/connection。
- 为兼容旧 Dify，`connection_id/target_connection_id` 仍可暂时提交，但只作为 legacy hint。默认 `ROUTE_LEGACY_HINT_MODE=warn`：不一致时记录日志并忽略客户端 connection。`strict` 时返回 409。
- 新 Session 把 `route_revision/route_binding_hash/account_scope_hash/connection_id` 写入内部 Session metadata；业务响应不暴露内部 connection。
- 既有 Session **不自动重路由**。如果旧 Session 本身是错误路由，例如 `gemini + aihubmix_default`，应新建 Session。
- Storage fallback 只影响“文件字节是否落 Relay Storage”，不会改变 Provider Wire 或 Inference Adapter。

## 新增关键日志

文件上传链：

```text
material_upload_received
material_request_parse_failed
material_request_validation_failed
material_policy_validation_failed
material_source_fetch_started
material_source_fetch_completed / material_source_fetch_failed
material_registry_create_failed
material_route_bound
provider_file_binding_started / completed / failed
material_fallback_store_started / completed / failed
material_ingress_completed
material_upload_completed / material_upload_failed
```

路由链：

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

所有关键错误都尽量带：`ingress_id/material_id/request_id/session_id/job_id`、`provider/model`、内部 `connection_id`（仅日志）、`route_revision`、`phase`、`duration_ms`、`http_status/upstream_http_status`、`failure_class`、`exception_type`。异常路径保留 traceback。

## 数据库

**0.5.0 -> 0.5.1 没有新增 SQL migration。** 路由冻结信息使用现有 `relay_sessions.metadata` 与 Request snapshot 保存。数据库仍需已完成 0.4.0 的 `sql/004_provider_native_file_ingress.sql`。

详细部署与调用示例见 `QUICKSTART.md`。
