# Model Relay 6.4.2 — Gemini 缓存未证实命中的完整上游响应日志

在 6.4.1 基础上，对已启用缓存但没有 Provider 命中证据的 Gemini 请求，记录 AIHubMix 响应头及完整原始响应实体。正文按片段写入日志，可依据请求 ID、片段序号及 SHA256 还原；敏感响应头隐去。API/Worker 均升级为 6.4.2；无需数据库迁移、额外模型调用或调用方改动。见 [RELEASE_NOTES_6.4.2.md](RELEASE_NOTES_6.4.2.md)。

# Model Relay 6.4.1 — Gemini 业务链内缓存诊断

在 6.4.0 基础上补齐最终请求分段指纹与上游 usage 形态日志，正常业务流程即可采集排障证据，无需独立 probe。不增加上游请求、不修改模型输入、缓存机制或命中判定。API/Worker 同时更新，无新增 SQL 或依赖。字段说明见 [RELEASE_NOTES_6.4.1.md](RELEASE_NOTES_6.4.1.md)。

# Model Relay 6.4.0 — 稳定材料跨阶段投影复用

以 6.3.0 完整源码为基线。新增独立材料指纹，以及 Gemini 合法 inlineData 材料投影的持久化复用；调用方无需改动。API/Worker 均为 6.4.0，无新 SQL、依赖或 tests 文件。结果 `cache.material_reuse` 与 Provider 的实际 hit 分别记录。实现范围、留存成本、兼容条件和部署步骤见 [RELEASE_NOTES_6.4.0.md](RELEASE_NOTES_6.4.0.md)。

# Model Relay 6.3.0 Gemini Auto 隐式缓存准入修正

本版以用户上传的 6.2.0 完整源码为唯一基线，为现有 AIHubMix Gemini 供应增加机制级 `provider_managed_trial`，允许 Auto 请求形成真实隐式前缀绑定，并保留原生 Usage 的 Hit、Miss、Unknown 诊断。

现有 Gemini 通配供应和已有精确供应均适用，未改路由匹配范围。试运行要求供给合同与新增的 `gemini-native-aihubmix-v2` Profile 双重授权，不能被当作账号实测认证或强制缓存权限。Provider 自行决定门槛和实际命中；本地不虚构 Token 下限或复用预测。

**部署须同时升级 API/Worker 并创建新 Session。** 旧会话保持冻结合同；外部控制面快照覆盖内置声明时，需按原治理流程发布新合同和 Profile。跨阶段调用方应保持稳定顶层 instructions，把阶段任务放在动态 input，Relay 不自动合并指令。

原有 125 项测试通过，补充的临时 Native 模拟验证通过，未新增或修改 tests 文件。真实 AIHubMix 命中尚需部署方按 Usage 实测验收。

完整变更、兼容边界和上线步骤见 [RELEASE_NOTES_6.3.0.md](RELEASE_NOTES_6.3.0.md)。以下为保留的历史版本说明。

---

# Model Relay 6.2.0 - Adaptive Explicit / Implicit Gemini Cache

## 6.2.0 缓存策略升级（以 6.1.0 完整源码为唯一基线）

API / Worker 版本为 `6.2.0`，新接受的 Cache Contract v3 Request 使用 `relay-cache-resolver/6`。本版保留 Session 隔离、Request 计划冻结、Gemini layout/9 物理投影及旧 resolver/3、/4、/5 已冻结 Request 的恢复语义；不新增 SQL migration，也不修改 layout/9 的消息投影。

### 统一计价模型

自动策略使用用户确认的相对价格，不把它冒充成 AIHubMix 的实时账单报价：

| 项目 | 相对普通 Input 价格 |
|---|---:|
| 普通 Input / 隐式缓存未命中 | `1.0 P` |
| Cache Read / 隐式缓存命中 | `0.1 P` |
| 显式 CachedContent 创建 | `1.0 P` |

对长度为 `T` 的稳定前缀、预计总读取次数 `N`、同 Session 实测隐式有效命中率 `h`：

```text
显式成本 = T × 1.0P + N × T × 0.1P
隐式成本 = N × T × (1 - 0.9h)P
```

实际执行时，成本门禁按边际可缓存 Token、冻结的预计复用次数和安全余量计算；`h` 来自同一 Session、相同 `reuse_evidence_key` 的 Provider usage。**隐式机制已认证但尚无实际命中样本时，不推测 `h`，自动选择隐式路径；只有拿到可核实的命中/未命中数据后才比较显式创建成本。**预测复用次数由最近成功请求的实际频率、样本新鲜度和缓存 TTL 保守推算，并受冻结策略上限约束。显式资源已有且精确兼容时直接复用，不重复计算过去的创建费用。

价格比例在 6.2.0 中固定为 `0.1` 和 `1.0`；治理层只能调整余量与观测预测参数：

```yaml
cache_read_input_ratio: 0.1
explicit_create_input_ratio: 1.0
safety_margin_ratio: 0.2
minimum_observations: 3
max_history_forecast: 8
history_forecast_confidence: 0.5
enable_observed_ttl_forecast: true
```

### `auto` 机制决策

1. **先验证同 Session 已有显式资源**：API 只读库存提示不能证明兼容；`StatefulResourceManager` 仍须核对 reuse key、前缀指纹、历史边界、有效期和 Provider `get` 结果。
2. **低复用、短生命周期或隐式命中率高**：优先选已认证的 `implicit_prefix`，不支付显式资源创建成本。
3. **高频复用且显式预期总成本更低**：优先尝试显式资源；精确 Token 数和硬预算在创建副作用前再次核验。
4. **显式创建被经济性或硬预算门禁拒绝**：若冻结 CacheIntentPlan 已包含经认证的隐式候选，则在首次模型派发前切换为 `implicit_prefix`。该切换是受理时冻结的候选，不是运行时临时发明；如果隐式前缀未命中，Provider 按普通 Input 处理，Request 仍正常执行。
5. **没有任何合法缓存机制**：保持完整上下文普通推理，不丢弃附件、历史或动态内容。

### 冻结与安全边界

- CacheIntentPlan 冻结主机制及允许的隐式回退候选；CacheExecutionBinding 在 `dispatch_started` 前最终封存。**派发开始后绝不切换机制、自动重发或重复创建资源。**
- 仅当主机制是 `stateful_resource`、Request 为 `auto`、回退原因属于明确的经济性/预算拒绝，并且冻结计划包含隐式候选时，才执行显式到隐式的回退。`cache_create_unknown` 等 Provider 副作用结果不确定的情况不会触发重试或经济性回退。
- 所有隐式投影仍要求具体 Supply 与 Protocol Profile 双重认证，Gemini 仍要求 layout/9 的完整物理投影及已验证的最小 Token 门槛。**本版不把当前内置 AIHubMix Gemini 合同自动标记为支持隐式缓存**；部署方完成该账号/通道端到端验证并发布新冻结合同/Profile 后，回退路径才会启用。
- 成本优化不计 TTL 存储费；但若治理策略设置 `max_new_resource_cost` 硬预算，仍必须提供足以核算创建费和存储费的冻结价格证据，不能因优化目标忽略存储而绕过硬预算。
- 新的 usage 记录包含 `effective_cache_mechanism`、`cache_read_tokens`、`uncached_input_tokens`、实际命中状态和复用身份；后续 Request 用这些观测更新命中率和复用预测。未知值保持未知，不从 Input Token 总量推断命中。

### 兼容性

旧 Request 已冻结的 resolver/3、resolver/4、resolver/5 仍按原计划恢复，不补写新的回退候选、不改变其经济性公式。Gemini layout/8 与 layout/9 的原有物理语义保持不变。无需新增 SQL migration，`relay_requests.cache_usage` 已有 JSONB 字段承载新增观测。

---

## 6.1.0 历史策略（保留原始版本说明）

### 6.1.0 缓存策略增强（以 6.0.0 完整源码为唯一基线）

本版本是 6.0.0 已批准缓存控制策略的处理方式增强，不重建框架或缓存资源体系。API/Worker 为 `6.1.0`，新接受的 Request 使用 `relay-cache-resolver/5`。**旧 Request 已冻结的 resolver/4 成本门禁和 Gemini layout/8、layout/9 物理语义保持不变**，现有 Session 不自动升级；不修改 SQL、tests、`.git` 或隐藏文件。

### `auto` 机制优先级

1. **同 Session 已有显式资源优先考虑。** API 受理阶段从相同 Session、Supply/connection/account scope、Protocol Profile 的只读历史和 ready 资源读出一个**候选线索**，新 Request 将线索冻结进 CacheIntentPlan，Resolver 可以据此优先选显式机制。线索不是“已经命中”的证明；`StatefulResourceManager` 在模型派发前仍按真实物理 `reuse_key`、前缀指纹、历史版本、过期状态以及 `get` 结果验证。未匹配成功时绝不使用错误资源；新资源仍受创建经济性门禁约束。
2. **大型稳定附件优先评估显式。** 只对 Canonical Material 中带内容摘要、已经属于本次 `conversation` Session 稳定材料清单的附件生成候选信号；默认累计原始附件大小达到 1 MiB（冻结的 `cache_policy.metadata.auto_economics.large_attachment_min_bytes` 可调整）。**原始字节只用于候选优先级，不代替 Gemini `countTokens`**。供应和 Profile 都认证了机制、当前读取收益与独立创建费用核算为正时才尝试新建 CachedContent。大文件仍受 AIHubMix 70 MiB `inlineData` 选择规则及完整缓存创建请求体限制；其余附件必须完整作为推理输入。
3. **动态历史及已提交模型输出优先隐式。** 当无资源候选或合算的大附件时，已认证的 `implicit_prefix` 是 Gemini `auto` 的默认机制。layout/9 已经投影按原序排列的完整可证明历史前缀，结果是否命中由 Provider usage 证明；未认证的 AIHubMix 隐式机制绝不伪装成已认证，也不把普通推理的透明命中当作受控隐式绑定。
4. **一次 Request 仍然只有一个主机制。** 受理冻结 CacheIntentPlan，执行时按该计划准备并冻结 CacheExecutionBinding，首次 `dispatch_started` 后不自动切换或重发；Provider Adapter 不自行改变机制。保留 `off/auto/on` 与显式指定 `requested_cache_mechanism` 的原语义、Session 内资源隔离和 `context_policy` 上下文语义。

### 新建显式资源的当前读取成本门禁

`resolver/5` 使用 `evaluate_current_read_resource`，根据 Provider `countTokens` 精确测得的前缀 Token、冻结的普通输入价、缓存读取价与**独立创建/写入收费**来比较**本次读取可节省费用**，并可加上经过策略认证的未来复用收益作为额外值。**当前收益为正即可创建，不要求预先预测下一轮会调用多少次**。TTL 存储价格仍可观测，但不进入此次自动优化目标。若配置了硬预算上限，仍必须有足以证明不会突破预算的完整费用数据，包括存储费：忽略存储费的优化偏好不能绕过预算安全。

- `price_schedule.currency`、`price_revision`、`input_cost_per_million_tokens`、`cache_read_cost_per_million_tokens`、`cache_write_cost_per_million_tokens` 使用**当前 AIHubMix 经验证的冻结价格**，且 `input_cost > cache_read_cost`；无法核实独立创建费不得自动猜作零。若确实没有独立创建费，可由治理层显式发布 `no_separate_creation_fee_verified: true`，不允许调用方修改。
- 若隐式路径已认证，还需由冻结策略提供可核实的 `estimated_implicit_hit_rate`（0–1）。缺失时不能声称显式优于隐式；该场景保留隐式默认。若实际供应只认证了显式，收益比较对照正常输入价格。
- 如果已存在兼容显式资源，优先直接复用，不把过去的创建费用重新计入本轮成本。历史增长默认不重建大前缀；只有治理策略明确 `enable_history_growth: true`、新增历史达到 `minimum_additional_tokens`（默认 4096 Token）、且相对已有资源的**边际当前读取收益**覆盖重建完整资源的收费时才创建新代次。
- `on + stateful_resource` 保留调用方明确指定的强制机制；不受自动正收益门槛制约，但仍受供应认证、物理投影正确性、文件大小、已冻结硬预算限制。`auto` 经济性或测量失败时仅按原先允许的完整上下文回退处理，不修改冻结 Session 和 Request 身份。

`cache_cost_decision` 返回当前读取收益、已有缓存覆盖、创建费用、仅观测的存储费用、未来额外收益与决策原因；`selection_evidence` 返回只读的 Session 资源候选线索和稳定附件字节指标，不含缓存句柄与文件内容。Gemini 真实 `promptTokenCount` 仍包含缓存读取 Token，计费估算将其拆分为缓存读取和未缓存输入，避免按普通输入重复核算；6.1.0 的 cost observation 在价格可用时提供**不含存储费**的金额，不能误称为总账单。

### 尚未启用的能力与外部认证条件

当前 AIHubMix 内置 Gemini 供应合同**尚未独立认证受控隐式前缀**。需要对具体账号/通道的请求结构、门槛、用量和费用进行端到端测试，再通过治理配置快照发布；6.1.0 不会自行将其标为 `verified`。

`context_policy="explicit"` 中 Request-only 附件若与动态文本位于同一条 Gemini User Content，layout/9 不能证明把附件提升到单独 CachedContent 后的消息语义不变，故**仍保持原消息完整，不强行改写**。本次不新增未经验证的 layout/10，也不承诺这种附件已经显式缓存。此前 AIHubMix 已验证不支持通过 File API URI 建立显式缓存，继续只通过 Relay `inlineData` 建立缓存资源。

---

# Model Relay 6.0.0 - Governed Gemini Cache Mechanism & Economics

## 6.0.0 变更（新缓存控制需求）

本版基于用户原始 `model-relay-5.0.0-source-final.zip` 的完整副本恢复已交付的 5.1.1 修复后实施，保留 `.git`、全部隐藏文件及原有测试文件；不增加 SQL migration。API/Worker 均为 `6.0.0`。现有 Gemini layout/8 与 layout/9 的冻结物理语义均不变。

### 请求接口（仅 cache contract v3 Session）

`requested_cache_mode` 保持 `off | auto | on`；新增 `requested_cache_mechanism`，允许 `auto | implicit_prefix | stateful_resource`，默认 `auto`。仅明确提供新字段时，才改变旧 Request 的 caller-intent hash 身份规则；相同 Idempotency-Key 改变该字段会产生 `IDEMPOTENCY_CONFLICT`。旧非 v3 Session 明确使用任一缓存控制参数时仍需创建新 Session。

```json
{
  "input": "本轮问题",
  "execution": {"mode": "sync"},
  "requested_cache_mode": "on",
  "requested_cache_mechanism": "implicit_prefix"
}
```

- `off + auto`：Relay 不主动创建或引用资源；供应商透明隐式命中仍可能发生。`off + 指定机制` 会拒绝。
- `auto + auto`：若**具体 Model Supply 与 Protocol Profile 均有已验证的隐式投影机制**，优先选择无显式创建开销的隐式路径；否则按冻结资格选择其它受支持路径。已有兼容显式资源仍可复用。
- `auto + implicit_prefix`：仅尝试符合资格的隐式投影，不会转而创建显式缓存。
- `auto + stateful_resource`：软偏好；当已认证的隐式路径可用、且缺乏未来复用依据时改选隐式，否则仅在创建前经济性门禁通过时创建新资源；无价格证据则跳过创建，保留完整模型上下文。
- `on + implicit_prefix`：强制**合法隐式投影**；不保证供应商实际命中，且无法达到认证门槛时在首次模型派发前拒绝。
- `on + stateful_resource`：要求复用或创建合格显式资源。用户可明确要求承担创建成本，但依然不得突破冻结的硬预算、权限、Provider 能力和 70 MiB 内联限制。
- `on + auto`：由 Relay 从已批准机制中选择，若调用方**显式提供**了新机制字段（即使填 `auto`），强制资格不可满足时明确拒绝；完全省略新字段的旧 v3 Session 保留原始“已确定低于门槛则可不缓存继续”的冻结兼容语义。

**重要部署前提：** 现有内置 AIHubMix Gemini Native Protocol Profile 仅登记已验证的显式 `stateful_resource`。本次不会仅凭官方文档宣称 AIHubMix 隐式路径已获独立认证，也不会偷偷将它加入已发布配置。要让 Gemini 默认选择受控隐式前缀，须先在该具体供应/账号完成隐式投影与用量认证，再通过治理控制面发布**同时**声明 `implicit_prefix` 的冻结 Capability Contract 和 Protocol Profile；未认证情况下 Provider 自发透明命中仅作观测，不被声明为 Relay 受控机制。

### 自动新建显式资源的受控成本门禁

`auto` 下若已有同 Session、同供应、同前缀身份的兼容显式资源，优先复用；否则**在发起 `CachedContent.create` 前**用该通道 `countTokens` 测得的缓存前缀 Tokens、冻结成本表、TTL、历史复用证据和保守未来预期计算净收益。缺少完整价格或无法证明净收益为正时，不创建资源，完整的请求上下文仍作为普通推理输入。`on` 的显式硬预算仍必须符合约束；未知费用又存在硬上限时拒绝，不猜测为零。

成本规则以 `cache_policy.metadata.auto_economics` 表达，必须通过配置治理发布、随 Session / Request 冻结，调用方 `metadata` 不能注入或覆写。例如：

```json
{
  "metadata": {
    "auto_economics": {
      "expected_future_reuses": 0,
      "allow_history_forecast": false,
      "minimum_observations": 3,
      "max_history_forecast": 2,
      "estimated_implicit_hit_rate": 0.65,
      "safety_margin_ratio": 0.2,
      "max_new_resource_cost": 0.03,
      "enable_history_growth": false,
      "price_schedule": {
        "currency": "USD",
        "price_revision": "verified-aihubmix-price-revision",
        "input_cost_per_million_tokens": 2.0,
        "cache_read_cost_per_million_tokens": 0.2,
        "cache_write_cost_per_million_tokens": 1.0,
        "storage_cost_per_million_tokens_minute": 0.02
      }
    }
  }
}
```

**以上价格仅用于展示字段格式，绝不能当成真实 AIHubMix 价格发布。** `expected_future_reuses` 是管理侧可审计预测，不是调用方参数。`allow_history_forecast` 默认为 `false`；如经审批开启，只从同 Session 最近已成功且基座身份相同的请求读取有限样本，达到最小样本数才给出非常保守的未来估计；无法查询则视为 0，不臆测。创建成本包括新前缀完整写入、TTL 存储，收益则与经认证的隐式命中率或普通输入作比较，留出风险余量。

`enable_history_growth` 必须显式由冻结策略开启，才允许在经济性成立时将已提交完整历史轮次（含以前的模型输出）形成更长的**新**不可变快照。只有在有效历史前缀和投影等价性已验证时才能增长；没有创建证据时继续使用旧资源与未缓存后缀。滚动隐式前缀继续由 layout/9 的 provider physical plan 保持历史输入与模型输出的合法顺序；不伪造 provider 命中。

### 安全不变量及观测

控制面仍冻结 RouteBinding、Capability、Protocol Profile 和 Cache Policy；`CacheIntentResolver` 只解释冻结计划，`StatefulResourceManager` 在一次真正的资源创建副作用前执行成本门禁，`CacheOrchestrator` 不重选已冻结主机制，Adapter 只投影最终绑定。`CacheExecutionBinding` 仍在 `dispatch_started` 前封存；未知 Provider side effect 不得自动重试。缓存作用域继续包含 `session_id`，没有跨 Session 复用。70 MiB AIHubMix 内联缓存安全预算与原 Files API 推理传输策略完全保留。

返回摘要包括 `requested_cache_mechanism`、`estimated_future_reuses`、`reuse_evidence_source`、`cache_cost_decision`，保留 Provider 实际的 `cache_read_tokens` / `uncached_input_tokens`。所有预测与实际命中分离，不承诺隐式命中，也不把显式缓存创建费用当成生成模型的输出 Token。

---

# Model Relay 5.1.1 - Gemini Layout/9 Cache Correctness Hotfix

## 5.1.1 Bug 修复

本版本以原始 5.1.0 为唯一基线，仅修复 Gemini layout/9 的三个正确性问题；不变更 API、Fusion、Session 隔离、70 MiB 内联缓存预算、缓存资源作用域或首次 dispatch 之后的恢复边界。

- **修复适配器 `NameError`**：`gemini_native.py` 补齐 `GEMINI_PHYSICAL_LAYOUT_VERSION_V9` 常量导入，确保无缓存、显式和隐式请求均可通过适配器的布局检查。
- **保留未缓存历史附件**：layout/9 只从历史投影中移除已经进入冻结缓存前缀的 Session 附件。未被缓存的附件保留在原历史 Content 中，同时禁止含推理专用附件的历史轮次进入显式缓存资源前缀；不改旧 layout/8 及更早布局。
- **防止 explicit Request 附件结构变化**：Gemini 当前协议无法证明将同一 `user Content` 中的 Request-only 附件和动态正文拆成两个 `user Contents` 等价。layout/9 对这种候选采取安全降级，不将 Request-only 附件提升为显式资源前缀；原始单条用户消息及其附件仍完整发送。隐式投影与无缓存投影必须维持完整的原始消息结构。此限制不影响 conversation 已经确认的 Session 稳定材料候选。

### 版本与兼容性

- API / Worker：`5.1.1`；Gemini Adapter 和物理 layout/projector 仍使用既有的 `/8` 与 `/9` 标识，不变更 API 或数据库表结构。
- 原 layout/8 及更早版本保持原有冻结语义；修复仅针对 layout/9 新行为及共享适配器中的遗漏导入。
- 不增加 SQL migration，不改测试文件。Provider 原始响应审计与既有脱敏测试冲突不属于本次修改范围。

---

# Model Relay 5.1.0 - Gemini Cache Prefix Planning and Usage Observability

## 5.1.0 版本定位

本版本在保留 5.0.0 既有架构、Session 隔离和执行恢复语义的前提下，增强 Gemini 缓存前缀规划与观测能力。新 Session 冻结 `gemini-physical-cache-layout/9`，旧 layout/8 及更早布局保持原有语义，不静默升级。

### 主要改造

- **统一前缀规划**：layout/9 将稳定材料与连续、可安全投影的已提交历史组成版本化前缀；按历史版本生成兼容指纹和对应未缓存后缀，优先复用同一 Session 内最长的合法资源前缀。
- **显式上下文材料候选**：`context_policy="explicit"` 不继承历史，但本次 Request 明确提供的材料可以成为缓存候选；资源仍严格限定在同一 Session 作用域。
- **显式资源增长成本门禁**：只有冻结的 `prefix_growth` 策略提供未来复用次数、同币种价格版本、输入/缓存读取/写入/TTL 存储价格，并且预期节省大于新资源创建与 TTL 存储成本时，才创建更长的不可变资源代次。缺少价格或复用预测时继续复用现有前缀，不假定成本为零。
  `prefix_growth` 至少需要 `enabled`、`expected_future_reuses`、`currency`、`price_revision`、`input_cost_per_million_tokens`、`cache_read_cost_per_million_tokens`、`cache_write_cost_per_million_tokens` 和 `storage_cost_per_million_tokens_minute`；可选 `minimum_additional_tokens`。各费率必须使用同一币种与明确价格版本。
- **隐式前缀投影**：Gemini 隐式前缀使用独立 binding 和 layout/9 稳定前缀投影，不创建 `CachedContent`、不发送 prompt-cache key；门槛未知时必须使用冻结的 `countTokens` guard 或可靠的上下文区间，不能把未知当作已满足。
- **请求体大小保护**：保留 AIHubMix 70 MiB 材料选择规则，并在完整序列化 CachedContent 创建请求上执行 100,000,000 字节保护；历史前缀按完整 turn 边界截断到最长可安全缓存的版本，不裁剪模型推理上下文。
- **用量观测**：Gemini Native 归一化 `promptTokenCount`、`cachedContentTokenCount`、未缓存输入量和缓存读取比例；同时记录资源创建/复用动作、前缀版本、generation 与内容指纹。存在完整冻结价格表时输出输入与缓存成本估算；没有价格版本或供应商账单证据时明确标记不可用，不伪造实际成本。

### 版本与边界

- API / Worker：`5.1.0`
- Gemini inference adapter：`gemini-native-aihubmix/8`
- Gemini cache adapter：`gemini-cache-aihubmix/9`
- Gemini physical layout / projector：`9` / `9`
- Cache material projection：`gemini-cache-material-projection/3`
- Resolver：`relay-cache-resolver/3`
- 无 SQL migration；沿用现有 `prefix_version`、generation、operation fencing 和 Session scope 字段。
- 本版本不启用同一 Request 的显式+隐式混合执行，也不增加跨 Session 内容复用。隐式缓存只有在具体 Model Supply / Protocol Profile 完成认证并通过真实供应商验证后才能配置启用。

---

# Model Relay 5.0.0 - Gemini Cache Admission and Diagnostics

## 5.0.0 版本定位

本版本在保留既有 Session / Request、缓存资源副作用隔离和故障恢复语义的前提下，调整 Gemini 缓存准入与诊断：Relay 对已声明具体缓存机制的 `gemini-*` 允许系统侧尝试，不把该例外视为供应商能力已认证；缓存协议投影、缓存策略、物理布局和 Adapter 约束仍然生效。缓存创建/验证失败会在受控缓存操作记录中保留供应商原始响应实体（Base64、长度、SHA-256、HTTP 状态、请求 ID 和阶段），普通日志继续使用脱敏诊断摘要。Gemini 物理缓存计划日志新增指令、稳定前缀、材料表示和复用身份的指纹，辅助定位身份变化来源。

4.3.2 修复 4.3.1 的 layout/7 执行层回归：API 已选择 `<70 MiB -> inline_cache_no_files`，但 `SharedExecutionRuntime` 仍只对 layout/6 启用 split inline-cache binding，导致 layout/7 可能回落到历史 Files-first 路径，并在 `contents` 为空时错误尝试创建 CachedContent。

本版按改造方案的职责边界处理，不在 Provider Adapter 内临时改策略：**Session 冻结新 layout/8，Runtime 按冻结 layout 选择 Material Binding，Projector 只生成合法 CacheSpec，StatefulResourceManager 只对 `cacheable=true` 的规格执行资源副作用。**

### 三层修复

- **修正 layout 判定**：Runtime 改用 `uses_inline_cache_transport_layout()`，新 layout/8 正确进入 `<70 MiB / >=70 MiB` split material 策略；layout/6 保持历史支持。
- **阻止空 Contents 创建**：layout/8 只有 `cached_prefix.contents` 非空才允许 `cacheable=true`。`systemInstruction` 继续进入 CachedContent identity，但不能单独触发 `caches.create()`；Cache Adapter 再做一次边界校验，并在 `create()` 加硬保护。
- **新 layout 隔离**：新 Session 冻结 `gemini-physical-cache-layout/8` / `gemini-physical-projector/8`，CacheSpec 升为 `/8`。旧 layout/7 Session 不静默升级，历史 `cache_create_unknown` 不会污染新 layout/8 identity。

### 版本

- API / Worker：`5.0.0`
- Gemini inference adapter：`gemini-native-aihubmix/7`
- Gemini cache adapter：`gemini-cache-aihubmix/8`
- Gemini physical layout/projector：`8` / `8`
- 无 SQL migration

部署见 `DEPLOYMENT_4.3.2.md`，验证见 `VALIDATION_4.3.2.md`，改动见 `CHANGELOG_4.3.2.md`。

---

# Model Relay 4.3.1 - Gemini Cached Instruction Freeze

## 4.3.1 版本定位

4.3.1 修复 4.3.0 中 **CachedContent 已创建成功，但 `generateContent` 同时发送 `cachedContent + systemInstruction` 被 AIHubMix 400 拒绝**的问题。

新 `layout/7` 把 Request `instructions` 固化进 CachedContent，并把它纳入 cache fingerprint / reuse key。命中缓存后，动态推理只发送 `cachedContent`、增量 `contents` 和允许的 `generationConfig`；最终 wire 会在所有 option / structured-output 投影之后再次剔除 `systemInstruction`、`tools`、`toolConfig`。

### 核心规则

- 新 Session：`gemini-physical-cache-layout/7` / `gemini-physical-projector/7`。
- `instructions` 进入 `caches.create(... system_instruction=...)`，不再进入 cached inference suffix。
- 不同 `instructions` 产生不同 `content_fingerprint` / `reuse_key`，禁止错误复用 Cache。
- cached inference 最终 JSON 中绝不保留顶层 `systemInstruction` / `tools` / `toolConfig`。
- cache 在 dispatch 前降级时，`full_uncached_payload` 仍恢复完整 `systemInstruction`，不会丢逻辑上下文。
- 4.3.0 的 `<70 MiB / >=70 MiB` 文件分流策略保持不变。
- 旧 layout/6 Session 不静默升级；要使用修复后的语义必须创建新 Session。
- API / Worker：`4.3.1`；Gemini inference adapter：`gemini-native-aihubmix/6`；cache adapter：`gemini-cache-aihubmix/7`；无 SQL migration。

部署见 `DEPLOYMENT_4.3.1.md`，验证见 `VALIDATION_4.3.1.md`，改动见 `CHANGELOG_4.3.1.md`。

---

# Model Relay 4.3.0 - Gemini Inline Cache / Files Inference Split

## 4.3.0 版本定位

4.3.0 暂停使用 `Gemini Files API -> caches.create(File)` 作为新 Session 的缓存构造方式。新 `layout/6` 将 **CachedContent 与 Files API 完全解耦**：缓存只接收 Relay 持有的静态 bytes（`inlineData`）；Gemini Files API 只服务于 `>=70 MiB` 分支中未进入缓存的小/大文件剩余集合的 inference。

### 核心规则

- Gemini `inference_input` 上传时先进入 Relay staging，不在 ingress 阶段调用 Files API。
- Request 冻结时以 Relay 实际 Material bytes 重新计算总量。
- **总量 <70 MiB**：不使用 Gemini Files API；Session-stable 静态材料单点注入 CachedContent，命中后只发送 `cachedContent` 引用 + 动态增量。
- **总量 >=70 MiB**：按 `(size_bytes, material_id)` 决定小文件缓存子集，累计严格 `<70 MiB`；这些文件仍以 inline bytes 单点注入 Cache。其他剩余材料只用于 inference，优先 Gemini Files API，失败后 Supabase Signed External URL fallback。
- External URL / Gemini File 都不会进入 layout/6 CachedContent。
- 新 Session：`gemini-physical-cache-layout/6` / `gemini-physical-projector/6`。
- 旧 layout/5 Session 保留 4.2.x 冻结语义，不静默升级。
- API / Worker：`4.3.0`；无 SQL migration。

部署见 `DEPLOYMENT_4.3.0.md`，验证见 `VALIDATION_4.3.0.md`，改动见 `CHANGELOG_4.3.0.md`。

---

# Model Relay 4.2.2 - AIHubMix Gemini Cache Raw HTTP Diagnostics

## 4.2.2 版本定位

4.2.2 不改变 layout/5 缓存语义，只增强 `google-genai` `caches.create()` 失败诊断。SDK 抛出 4xx/5xx 时，Relay 优先抓取底层 HTTP response 的原始 entity bytes，而不是只重新序列化 `response_json`。`cache_create_provider_http_error` 现在会带上`raw_body_text`、原始 body 来源、body SHA-256、content type、无 query 的请求 URL，以及经过 allowlist 的 trace/request headers；Authorization/API key/Cookie 不进入日志。

- API / Worker 版本：`4.2.2`。
- `google-genai` 内部 adapter：`google-genai-aihubmix/2`。
- 无 SQL 变更。
- 部署见 `DEPLOYMENT_4.2.2.md`，验证见 `VALIDATION_4.2.2.md`，改动见 `CHANGELOG_4.2.2.md`。

---

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


## V2 workbook-driven governed runtime settings

`config/model_management_v2.xlsx` is the operator-facing declaration workbook.
It is no longer a test fixture. Use the offline governance compiler and
external approval process in [`config/README.md`](config/README.md) to deploy
a SHA256-pinned `MODEL_CONTROL_PLANE_FILE` snapshot. Directly editing Excel
does not activate new models or channels.
