# Model Relay 4.2.1 - AIHubMix Gemini SDK Gateway Hotfix

## 4.2.1 版本定位

4.2.1 修复 4.2.0 中 AIHubMix `google-genai` 自定义 base URL 的错误配置。AIHubMix 当前官方文档要求 SDK base URL 固定为 `https://aihubmix.com/gemini`；4.2.0 误用了站点根 `https://aihubmix.com`，导致 `files.upload()` 与 `caches.create()` 命中错误网关并返回 `POST object expects Content-Type multipart/form-data`。

### 4.2.1 核心规则

- `AIHUBMIX_GEMINI_SDK_BASE_URL=https://aihubmix.com/gemini`。
- `Settings` 默认值同步修正为 `/gemini`。
- 对 4.2.0 已部署的精确旧值 `https://aihubmix.com` 做窄范围兼容归一化，运行时自动转成 `https://aihubmix.com/gemini`；自定义代理地址不改写。
- `gemini-physical-cache-layout/5`、Files API -> SDK `File` -> `caches.create()` -> Relay binding/seal -> `generateContent(cachedContent=...)` 的业务语义不变。
- 无 SQL 变更。API / Worker 版本为 `4.2.1`。
- 为验证 File 对象直传链路，升级后应重新上传 Gemini material，并新建 Session。

部署见 `DEPLOYMENT_4.2.1.md`，验证见 `VALIDATION_4.2.1.md`，改动见 `CHANGELOG_4.2.1.md`。

---

# Model Relay 4.2.0 - AIHubMix `google-genai` Files + Context Cache

> 历史版本提示：4.2.0 文档中的 `AIHUBMIX_GEMINI_SDK_BASE_URL=https://aihubmix.com` 是错误配置，已由 4.2.1 修复。不要按该旧值部署。

4.2.0 首次将 AIHubMix Gemini Files 上传与 layout/5 Context Cache 创建切到 `google-genai` SDK；其设计目标和 layout/5 语义由 4.2.1 保留。

---

# Model Relay 4.1.0 - AIHubMix Native Cache Create Flow

## 4.1.0 版本定位

本版按项目 `XX.YY.ZZ` 规则属于对 4.0.0 **缓存处理方式的纠正/增强**，因此升级 YY：**4.0.0 -> 4.1.0**，ZZ 清零。4.0.0 的 Files API first、Cache/Inference Material 分离和 70 MiB fallback 全部保留；本版只收紧 Gemini Stateful Cache 的创建主链。

### 4.1.0 核心规则

- **新 Session 不再前置 `countTokens`**：layout/4 直接执行 `Gemini Files API -> cachedContents.create -> generateContent(cachedContent=...)`。
- **Provider create 决定门槛**：不再由 Relay 用硬编码 `1024` token 做当前 Gemini 缓存资格判断。`caches.create()` 成功即表示 Provider 接受该精确缓存前缀；确定性 create 4xx 再按 `auto/on` 的既有策略处理。
- **副作用控制不交给 SDK**：调用语义和 Native wire 对齐 AIHubMix/Google GenAI SDK 示例，但 Relay 仍由自己的 HTTP Adapter 发请求，以保留 cache-operation ledger、lease/fencing、Provider request-id、错误脱敏与 ambiguous-create 防重放语义。
- **Files-first 不变**：文件优先上传 Gemini Files API；上传失败后仍按 `<70 MiB / >=70 MiB` 规则选择 `inlineData` 缓存子集，其余 Supabase External URL 只进入 inference。
- **两级冻结不变**：Cache logical plan、Gemini physical plan、CacheExecutionBinding 仍在模型 dispatch 前冻结/seal；模型进入 `dispatch_started` 后不允许通过去缓存或换 transport 重发。
- **旧 Session 不迁移**：4.0.0 `gemini-physical-cache-layout/3` 继续执行被冻结的 `countTokens` preflight；要使用新链路必须创建新 Gemini Session。
- 新 Session 冻结 `gemini-physical-cache-layout/4` / `gemini-physical-projector/4`；无新增 SQL migration。

部署见 `DEPLOYMENT_4.1.0.md`，验证见 `VALIDATION_4.1.0.md`，改动明细见 `CHANGELOG_4.1.0.md`。

---

# Model Relay 4.0.0 - Gemini Files-First Cache Material Projection

## 4.0.0 版本定位

本版按项目 `XX.YY.ZZ` 规则属于**新修改需求**，因此从 3.2.0 升级到 **4.0.0**。核心变化不是改变 Canonical Material / Session / Request，而是把 Gemini 的 **Inference Material Binding** 与 **Cache Material Projection** 真正拆开：文件上传先尝试 Gemini Files API；只有 Files API 失败后才使用 Supabase 作为 inference fallback，并按 70 MiB 规则决定哪些静态材料可以单点注入 CachedContent。

### 4.0.0 核心规则

- **Files API first**：Gemini `inference_input` 不再按 99 MiB 在 Supabase/Files 之间先选；每个文件都先尝试 Gemini Files API。成功得到的 `gemini_file_uri` 可直接进入 CachedContent。
- **Files 失败才用 Supabase**：失败后原始 bytes 写入 Relay fallback storage，Signed External URL 只作为 inference binding，不允许进入 CachedContent。
- **总量 <70 MiB**：Files 失败的 Session-stable 静态材料全部以 `inlineData` 单点注入 CachedContent；后续命中时只发送 `cachedContent` 引用 + 动态增量。
- **总量 >=70 MiB**：对 Files 失败的 Session-stable 材料按 `(size_bytes, material_id)` 确定性小文件优先，选择累计原始字节严格 `<70 MiB` 的子集注入 CachedContent；其余大文件保留 Supabase External URL，只进入 inference uncached suffix，不参与缓存。
- **两套执行投影**：`material_binding_snapshot` 继续冻结 inference 表示；layout/3 额外形成 cache-only material projection。`full_uncached_payload` 仍完整保留，因此缓存关闭、门槛不足或 pre-dispatch 降级不会丢材料。
- **恢复边界不变**：缓存资源副作用仍发生在模型 `dispatch_started` 之前；进入模型 dispatch 后不允许因为缓存问题换 transport 或重新推理。
- 新 Gemini Session 冻结 `gemini-physical-cache-layout/3`；layout/2 Session 不静默升级。无新增 SQL migration。

部署见 `DEPLOYMENT_4.0.0.md`，验证见 `VALIDATION_4.0.0.md`，改动明细见 `CHANGELOG_4.0.0.md`。

---

# Model Relay 3.2.0 - Gemini Physical Cache Projection

## 3.2.0 版本定位

以 3.1.0 为基线，本版把 Gemini 缓存执行收紧到统一的、按 Session 冻结版本的 `GeminiPhysicalCachePlan`。Canonical Session / Material / History 不变；新的 Provider-facing layout 将 Session Material 真正放入 CachedContent，并保证 `countTokens -> CacheSpec fingerprint -> CachedContent create -> generateContent` 消费同一份物理计划。

### 3.2.0 核心变化

- 新 Gemini Session 冻结 `gemini-physical-cache-layout/2`；旧 Session 不回填，继续沿用 3.1 投影。
- `cached_prefix` 承载 Session stable Material；`uncached_suffix` 承载 request/stage instruction、projected dynamic history、current input。
- Material occurrence mapping 只改变 Gemini 物理投影，不改 canonical history；缓存命中时不会重复发送 Session Material。
- 新布局严格先执行 `countTokens(exact cached_prefix)`，低于 minimum 时 final mechanism=None，并发送完整 uncached context。
- `ProviderHTTPError.body/request_id/status/phase` 先脱敏/伪匿名再写 cache operation ledger 与结构化日志；模型推理 ProviderHTTPError 结构化日志也使用相同安全观察。
- 创建未拿到 cache handle 时 operation 进入 `unknown`，打印 `cache_handle_unavailable_no_recreate`，本版**不做重新创建，也不新增 reconciler**。
- 无新增 SQL；继续要求 `sql/005_relay_cache_control.sql` + `sql/006_gemini_stateful_cache.sql`。

部署见 `DEPLOYMENT_3.2.0.md`，验证见 `VALIDATION_3.2.0.md`，改动明细见 `CHANGELOG_3.2.0.md`。

---

# Model Relay 3.1.0 - Gemini Stateful Cache Closure

## 3.1.0 版本定位

按项目版本规则，本次不是新增缓存需求，而是对 3.0.0 已定义的 Stateful Cache 处理方式做闭环增强，因此升级 **YY：3.0.0 -> 3.1.0**，ZZ 清零。

### 3.1.0 Gemini Stateful Cache 闭环

- 新增 `GeminiAIHubMixCacheResourceAdapter`，实现 Gemini Native `countTokens -> cachedContents.create -> get -> patch -> delete` Provider wire。
- API 与 Worker 都按冻结 connection 注册相同 Resource Adapter；不再出现 Planner 选中但执行侧无 Adapter 的假启用。
- `gemini-3.1-flash-lite` 与 `gemini-3.8-flash` 使用精确 ModelOffering + verified Cache Contract；泛 `gemini-*` 仍保持 candidate，禁止按协议名推断缓存能力。
- Provider token 计数作为最终门槛 Guard。Request 受理时可以冻结 `stateful_resource` 计划，执行前以 `countTokens` 确认是否达到 1024-token 显式缓存门槛；低于门槛时确定性 `effective=None` 后正常推理。
- Stateful Cache 只缓存 **system instruction + 已提交 conversation history 前缀**；当前轮 input/materials 永远留在 uncached suffix。
- 资源复用从“当前 history 全量指纹相同”改为“兼容的已提交历史前缀”。历史从 N 增长到 N+1 时，旧 generation 仍可复用，并只发送新增 history suffix + 当前输入。
- `GeminiNativeAdapter` 读取冻结的 CacheExecutionBinding；使用 Stateful Resource 时发送 `cachedContent=<handle>`，不重复发送已缓存 prefix/systemInstruction。
- Provider `usageMetadata.cachedContentTokenCount` 进入统一 cache usage，作为真实命中证据。
- 新增 `sql/006_gemini_stateful_cache.sql`：资源 `reuse_key/prefix_version/token_count/spec_hash`、cache-operation dispatch fence 与 v3.1 publish/failure/invalidate RPC；`running` 过期只会进入 `unknown`，不能授权重复 create。
- CachedContent handle 仅接受 `cachedContents/<id>` 相对资源名；Provider create 返回 handle 后先立即写 operation ledger，再执行 get 验证，最后才允许写入 ready resource ledger。
- `dispatch_started` 规则不变：模型请求一旦 sealed/dispatch 后，任何 cache 404/失效/usage 异常都不能触发“去缓存后再推理一次”。

部署前必须执行 `sql/006_gemini_stateful_cache.sql`，并为采用新 verified Supply 创建新 Session。详见 `DEPLOYMENT_3.1.0.md`。

---

## 3.0.0 版本定位

按项目版本规则，本次属于“新增模型缓存机制”的新修改需求，因此升级 **XX：2.0.0 -> 3.0.0**；YY/ZZ 同时清零。本版本在 2.0.0 的 Session / Request / Material / RouteBinding / Lease-Fencing 主链上增加缓存执行控制，不把缓存做成另一套模型执行系统。

### 3.0.0 已实现

- `/v2/sessions/{session_id}/requests` 新增 `requested_cache_mode=off|auto|on`；新 3.0 Session 省略时规范化为 `auto`。旧 Session 显式提交新缓存意图返回 `CACHE_CONTRACT_UPGRADE_REQUIRED`。
- 新增 `ProtocolProfileSpec`、`CachePolicySpec` 与 `CapabilityContract.cache`；缓存能力冻结到具体 ModelOffering / RouteBinding，不按模型名在 Core 中硬编码。
- 新增 `ContextPlan`、`CacheIntentResolver`、`CacheExecutionBinding`，统一抽象 `stateful_resource / breakpoint / implicit_prefix` 三类物理机制。
- Request contract 升级为 `relay-request/2.3`。新增 `caller_intent_hash`，并扩展 2.3 `request_hash` 覆盖 route/contract/profile/policy/context/cache plan；2.1/2.2 identity 算法保持原样。
- 新增 `sql/005_relay_cache_control.sql`：Cache Resource / Operation / Binding / Pin / Task，以及 sync Request execution fence、material binding fencing、cache binding install、atomic seal+dispatch、result-store/complete/fail v3 fencing。
- `SharedExecutionRuntime` 在 Material Binding 冻结后、Provider dispatch 前执行 CacheOrchestrator；v3 必须成功 `seal_cache_and_dispatch_v3` 才取得第一次模型发送权。
- Provider 返回后新增统一 cache usage 观察；`null` 与真实 `0` 分开，`off/None` 下 Provider 透明命中只记录 observed，不反写 Requested/Effective。
- Grok 保留既有 `stable_prompt_cache_key`、reasoning、encrypted history、`store=false` 行为。3.0 只增加外层 Gate：新合同解析为 None/off 时抑制可控 `prompt_cache_key`；on/auto 的 legacy-compatible implicit-prefix 仍使用原 key。
- Gemini / GPT / Claude 的缓存合同与协议 Profile 已进入控制面，但内置配置保持 `candidate`，不会因为“协议支持/已有 usage 字段”自动当成已认证缓存能力。真实 Stateful / Breakpoint / Prefix 启用必须逐 Supply 发布验证后的合同。
- Worker 同时识别 v2/v3 Request Job；旧 Request 与旧 Job 按原协议恢复。

### 3.0.0 明确边界

- 这不是回答结果缓存，不替代 Request 幂等、Session history 或 Canonical Material。
- `on` 遇到本次上下文/门槛不足时是 `effective=null` 后正常推理；只有供应无已认证机制、协议不可表达或政策禁止等合同问题才 Fail-Closed。
- `dispatch_started` 之后绝不因为缓存无效、404/429、未命中或回包异常执行“去缓存再推理一次”。
- 内置 Gemini / Claude / GPT 缓存 Profile 在真实渠道认证前保持 candidate；本版不会伪造已验证能力。
- 3.0.0 的 `prepared_payload_hash` 当前覆盖“冻结 snapshot + history + Material Binding + Cache Binding”的 pre-dispatch execution projection。Provider-specific byte-level Native Wire builder 可在后续处理方式增强版本中继续收紧，不改变 atomic dispatch gate。

部署、SQL 顺序、灰度和兼容说明见 `DEPLOYMENT_3.0.0.md`。

---

# Model Relay 2.0.0 - Governed Multi-Model Channels

## 2.0.0 关键变化

- 在既有 Session / Request / Material / RouteBinding 之上新增不可变 `ModelControlPlane` 运行时快照：模型、渠道/账号、协议、CapabilityContract、ModelOffering、按模型优先级分离治理。
- 首批内置接入 9 个 AIHubMix 目标模型；同协议模型复用 Responses / Chat Completions / Claude Messages / Gemini Native Adapter，不增加业务 Workflow 的模型分支。
- Session 可声明 `capability_requirements`；RouteResolver 先做能力/渠道/配置/Adapter 资格过滤，再按 Offering priority 选择，并冻结完整执行合同。
- 请求参数继续采用 requested -> effective 语义；未知参数、冲突能力、未验证思考控制 Fail-Closed。MiMo V2.6 Pro Free 当前仅开放 `think_level=auto`，不伪造未验证的上游 effort/toggle。
- AIHubMix 成功响应记录实际模型、router/fallback/JSON-repair 等可观测事实。精确 Offering 默认 strict：实际模型与冻结模型不一致时，保留原始 2xx 归档并返回合同失败，不自动换渠道重发。
- 自定义 `MODEL_CONTROL_PLANE_JSON` 只接受 `status=published`；API/Worker 暴露相同 revision/hash/status 供部署一致性检查。
- 既有 Request/Job、Lease/Fencing、`dispatch_started -> indeterminate`、Material/Binding 与 Structured Output canonical post-validation 不变；无 SQL migration。
- 免费模型 quota 当前仅为治理 metadata；分布式共享额度与 delayed queue 不在 2.0.0 内。

部署与自定义多渠道配置见 `DEPLOYMENT_2.0.0.md`。

---

# Model Relay 0.5.9 - Kimi K2.7 Forced Thinking ON

## 0.5.9 关键变化

- `kimi-k2.7-code*` 被建模为 **always-thinking**：无论 Dify / 调用方提交什么 `think_level` 文本值，Relay 都不再按深度枚举拒绝，而是保留原始 `requested_think_level` 仅作审计，并统一冻结为 `think_level=on`。
- `MoonshotChatAdapter` 对 `kimi-k2.7-code*` 固定发送原生 `thinking: {"type":"enabled"}`，确保 K2.7 Code 以 Thinking ON 执行；不会向该模型发送 `reasoning_effort`。
- 因为所有调用方思考深度最终执行语义完全相同，Request identity 使用同一 effective `think_level=on`；同一幂等键不会因为 `low/medium/high/max` 的 UI 选择不同而形成不同的 K2.7 执行语义。
- Kimi K3 的 `low/high/max -> reasoning_effort` 行为保持不变；其他 Kimi 模型继续按既有 capability profile Fail-Closed。
- `CAPABILITY_PROFILE_REVISION` 升级为 `relay-model-options/2026-09-23.4`，`MoonshotChatAdapter` 升级为 `moonshot-chat/5`。升级后必须创建新的 Kimi Session。
- 无 SQL migration；Session/Request/Job、Checkpoint/Resume、Material、Route、Structured Output 合同不变。

## 0.5.8 关键变化

- 修复 Dify 对 `kimi-k2.7-code` 发送 `think_level=low/high/max` 时被 Relay 422 `THINK_LEVEL_UNSUPPORTED` 拒绝的问题。
- `kimi-k2.7-code*` 现在接受 canonical `auto/low/high/max`。由于 K2.7 Code 为固定 Thinking ON，且官方接口没有公开该模型可调 `reasoning_effort` 合同，`low/high/max` 会保留为 `requested_think_level`，执行时规范化为 `think_level=auto`，不会向上游伪造不受支持的 effort 字段。
- Kimi K3 继续保持 `low/high/max -> reasoning_effort` 的原生 1:1 投影。
- `CAPABILITY_PROFILE_REVISION` 升级为 `relay-model-options/2026-09-23.3`，`MoonshotChatAdapter` 升级为 `moonshot-chat/4`。升级后请新建 Kimi Session。

---

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
