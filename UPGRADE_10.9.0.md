# Model Relay 10.9.0 续接与缓存策略优化

基于 `10.8.0` 基线与《架构机制说明》《续接改造方案》，在保持四层单向契约边界、`request_hash` 不含动态续接句柄、不可逆派发边界与既有账本不变量的前提下，本版本完成上下文续接默认策略、无状态前缀一致性与 `explicit` 单轮轻量执行路径三项核心优化。

## 变更摘要

### 1. 无状态/重放路径首轮顶层 `input` 结构一致与全协议载荷规范化
- **Gemini Interactions 首轮/多轮前缀对齐**：[`app/protocol_adapters/wire_projection/gemini_interactions_wire.py`](app/protocol_adapters/wire_projection/gemini_interactions_wire.py) 在 `context_policy=conversation`（非 `is_non_cumulative`）且处于无状态/重放路径（`store=false` 且具备 `relay-continuation-binding/2` 版本证明）时，自 Turn 1 起统一通过 `_format_delta_multi_turn_input(..., ordered_steps=True)` 输出 `[{"type": "user_input", "content": [...]}]`，保证首轮与后续重放轮次的 `input[0]` 结构字节完全一致，最大化命中上游隐式前缀缓存。
- **全协议 `conversation` 规范化常驻**：[`app/execution_plane/request_acceptor.py`](app/execution_plane/request_acceptor.py)、[`app/runtime/affinity.py`](app/runtime/affinity.py) 与 [`app/core/context_codec.py`](app/core/context_codec.py) 确保 `request_policy == "conversation"` 时始终执行 `canonical_structure`、`normalize_structured_output` 与物理载荷规范化（同时补充 `response_format.schema` 规范化），不因 `requested_cache_mode="off"` 破坏前缀稳定。
- **既有布局保留**：[`app/protocol_adapters/wire_projection/responses_wire.py`](app/protocol_adapters/wire_projection/responses_wire.py) 中 OpenAI 官方（顶层 `instructions`）与 xAI（`root_system_message` 置入 `input[0]`）布局继续保持稳定。

### 2. `context_policy=conversation` 默认开启续接与 `requested_cache_mode="auto"`
- **Opt-out 上游留存授权**：[`app/control_plane/upstream_retention.py`](app/control_plane/upstream_retention.py) 与 [`app/control_plane/governance/workbook.py`](app/control_plane/governance/workbook.py) 将 `gemini_interactions` 与 `responses` 的默认上游留存改为 Opt-out（未显式声明 `禁止` 或 `ZDR` 时默认 `allow_upstream_store=True`），仅对明确配置 `禁止`/`ZDR` 或命中 `ZERO_DATA_RETENTION_TENANTS` 的租户关闭 `store: true`。
- **控制面与工作簿默认策略**（`COMPILER_VERSION = "relay-excel/10.9.0"`）：
  - `workbook.py` 中 `gemini_interactions` 与 `responses` 供应默认续接模式改为 `prefer_stateful_anchor`（官方直连自动就绪；AIHubMix 等聚合渠道仍需通过启动探针 `stateful_continuation` facet 实测验证后才实际启用）；同时新增缓存机制 `'断点': 'breakpoint'` 映射，工作簿未显式强制缓存策略时默认模式改为 `'auto'`。
  - 更新 [`config/model_management_v2.xlsx`](config/model_management_v2.xlsx)：`连接配置` 页新增 `上游续接留存` 列并将 `google_gemini` 设为 `允许`；`缓存设置` 页将 `Google 官方` 全部 6 个 Gemini 模型统一启用 `隐式前缀 / 自动 / 4096 Token 门槛 / 原文无缓存继续`。
- **执行面全协议自动缓存默认值**：[`app/execution_plane/request_acceptor.py`](app/execution_plane/request_acceptor.py) 在 `request_policy == "conversation"` 且调用方未显式传入 `requested_cache_mode` 时，默认按 `"auto"` 受理（涵盖 `gemini_interactions`、`responses`、`claude_messages` `breakpoint`、`chat_completions`、`moonshot_chat`）。

### 3. `context_policy=explicit` 执行层轻量协议分流、小材料直读内联与跨厂商写入保护
- **请求执行层按轮动态分流至 `gemini_native`**：
  - [`app/execution_plane/request_acceptor.py`](app/execution_plane/request_acceptor.py) 对路由为 `gemini_interactions` 但本轮有效策略 `request_policy == "explicit"`（含独立 `explicit` 会话以及 `conversation` 会话内单轮传 `request.context_policy="explicit"`）的请求，在冻结快照 `base_snapshot` 中写入 `effective_wire_protocol = "gemini_native"`。
  - [`app/protocol_adapters/factory.py`](app/protocol_adapters/factory.py) 与 [`app/protocol_adapters/registry.py`](app/protocol_adapters/registry.py) 为 `gemini_interactions` 连接同池伴生注册共享同一 `ConnectionRuntime` 的 `GeminiNativeAdapter`，由 [`app/runtime/physical_context.py`](app/runtime/physical_context.py)（新增 `project_auxiliary_gemini_native`）与 [`app/runtime/controlled_invoker.py`](app/runtime/controlled_invoker.py) 直接派发至 `:generateContent`，免除 Interactions API 建链开销。
  - **跨协议混合历史与思考链无缝衔接**：[`app/protocol_adapters/profiles/gemini_native.py`](app/protocol_adapters/profiles/gemini_native.py) 过滤 `thought: true` 分块避免思维链文本混入 `result.text`，剥离 `modelVersion` 的 `models/` 前缀以通过 `strict` 模型核验，并在 `transport_history["model_content"]` 中完整留存 `thought` 与 `thoughtSignature`；[`app/core/material_placement.py`](app/core/material_placement.py)（`checked_history`）与 [`app/runtime/material_history.py`](app/runtime/material_history.py) 同步兼容 `gemini_interactions` 会话中穿插的 `gemini_native` 轮次并在后续 `conversation` 增量轮次忠实回放。
- **Gemini 与 Kimi (`moonshot_chat`) 小材料单次请求直读内联**：
  - [`app/config.py`](app/config.py) 新增 `auxiliary_inline_max_bytes`（环境变量 `AUXILIARY_INLINE_MAX_BYTES`，默认 `4 MiB`，上限 `20 MiB`）。
  - [`app/runtime/material_binder.py`](app/runtime/material_binder.py) 与 [`app/protocol_adapters/profiles/moonshot_chat.py`](app/protocol_adapters/profiles/moonshot_chat.py) 在 `explicit` 轮次下，直接从 Relay `FallbackObjectStorage` 读取不超过 `auxiliary_inline_max_bytes` 的材料字节进行单次请求内联（Gemini 投影为 `gemini_inline_data`，Kimi 官方渠道对 `text/*`、`application/json`、`application/xml` 投影为 `kimi_inline_text` / `purpose="inline-text"`），跳过 Gemini Files API 与 `KimiOfficialFileAdapter` 的上传、抽取与轮询往返。
- **跨厂商 `explicit` 单轮成本与历史隔离保护**：
  - [`app/execution_plane/cache_intent_resolver.py`](app/execution_plane/cache_intent_resolver.py) 在 `effective_context_policy == "explicit"` 且 `mode == "auto"` 时自动剔除会产生缓存写入溢价的 `breakpoint`（如 `claude_messages` 的 `cache_control` 断点）与 `stateful_resource`（Gemini 显式缓存创建）。
  - [`app/protocol_adapters/profiles/chat_completions.py`](app/protocol_adapters/profiles/chat_completions.py) 与 [`app/protocol_adapters/profiles/moonshot_chat.py`](app/protocol_adapters/profiles/moonshot_chat.py) 增加 `request_context_policy == "conversation"` 与 `model_history_visible is not False` 双重判定，防止在 `conversation` 会话中穿插 `explicit` 单轮时意外拼接历史消息。

## 部署与兼容说明

1. **无需新增数据库迁移**：沿用 `001`—`020` 数据库基线即可，同步使用 `10.9.0` 镜像部署 API 与 Worker。
2. **存量快照与新请求兼容**：升级前已受理或处于恢复路径的存量请求快照按原冻结字段执行；新受理请求及重新编译的 [`config/model_management_v2.xlsx`](config/model_management_v2.xlsx) 自动生效 `10.9.0` 规则。
3. **可选环境变量**：如需调整 `explicit` 辅助轮次小材料直读内联阈值，可配置 `AUXILIARY_INLINE_MAX_BYTES`（默认 `4194304`，即 `4 MiB`），API 与 Worker 须保持一致。
