# Model Relay 6.3.0 Gemini 隐式缓存准入修正

以用户上传的 `model-relay-6.2.0-source-final.zip` 为唯一源码基线。版本按现有缓存处理方式增强升级到 6.3.0。API 与 Worker 必须同时升级。本次没有 SQL migration，也没有新增或修改 tests 文件。

## 修复内容

原实现只向内置 Gemini Profile 声明显式资源能力；Resolver 又用整个缓存合同的认证状态过滤隐式候选。即使显式创建被经济性门禁跳过，也无法得到已冻结的隐式备选。

现在为现有 AIHubMix Gemini 通配供应及两个已有精确供应增加机制级 Auto 试运行声明，并新增 `gemini-native-aihubmix-v2` Profile。原 v1 Profile 定义保留，路由匹配规则、渠道集合和模型顺位不变。试运行覆盖现有 `ROUTE_GEMINI_MODEL_PATTERN`（默认 `gemini-*`）允许的模型，不限定 gemini-3.6-flash；该规则本身不被改写。

供给缓存子合同和 Protocol Profile 的 `cache.mechanism_profiles.implicit_prefix` 均须明确包含：

```json
{
  "verification_status": "trial",
  "auto_trial_enabled": true,
  "threshold_mode": "provider_managed_trial"
}
```

Resolver 将获准机制的 `admission_status=provider_managed_trial` 冻结进计划。试运行仅允许 `requested_cache_mode=auto`，不能通过指定 `implicit_prefix` 获得强制权限。机制级状态优先于整体状态：整体为 verified 不会把其中 trial 机制提升为已认证；整体为 candidate 也不会阻断已经明确获准的 Auto 试运行或独立已认证机制。原显式缓存认证与历史兼容入口保留。

建议中“全系列标记已认证”与“机制级受控试运行”口径冲突。本版采用全系列 Auto 试运行，没有制造账号级实测证据或把试运行写成认证。提升强制权限仍需发布经实际验证的机制合同和 Profile。

## 门槛与派发

`provider_managed_trial` 由 Provider 自行判断隐式缓存门槛。Relay 不编造最小 Token 数、复用次数或命中率，也不为这条试运行路径额外调用 countTokens 或创建 CachedContent。存在合法稳定上下文且 layout/9 完整投影可用时，Orchestrator 核验冻结的双重授权，形成 finalized 绑定；门槛不足或上游不缓存时仍是一条完整的普通输入请求。

新机制级合同生成 `relay-cache-resolver/7` 计划。旧合同继续生成 resolver/6 计划；原已受理的 /3、/4、/5、/6 Request 不被改写。/7 继续使用 6.2.0 的相对价格、经济性和硬预算门禁。经济性记录分别保存 certified 与 trial 的隐式备选资格，不将试运行伪装为认证。

已有兼容显式资源仍优先复用。若选择显式主机制，合法隐式备选在受理时一并冻结，显式准备仅因允许的经济性或预算原因被拒绝时，可在首次派发前使用该备选。`create_unknown` 不触发机制回退或再次创建；`dispatch_started` 后不增加模型重发、跨渠道切换或缓存机制切换。

## Usage 与诊断

Gemini Native 的 `gemini_cache_usage_observed` 日志和结果缓存摘要记录：

- `planned_mechanism`、`effective_cache_mechanism`。
- `implicit_prefix_fingerprint`、`payload_hash`。
- `cache_read_tokens`、`actual_cache_hit_status`、`admission_status`。
- Native 诊断日志另记录 `cached_content_token_count_present`。

`payload_hash` 是投影、模型参数和结构化输出处理后的完整 payload 的规范 JSON 摘要，不是最终 HTTP 字节签名。日志不写入请求正文、材料内容、凭据或资源句柄。

Provider 返回 `cachedContentTokenCount > 0` 才记录 Hit；明确返回 0 记录 Miss；未返回该字段记录 Unknown，缓存关闭时也不把缺失字段当成零。透明命中仍独立标记。`effective_cache_mechanism=implicit_prefix` 只证明 Relay 已执行合法布局，不证明 Provider 命中。

## 部署与真实验收

1. 同时部署 6.3.0 API 与 Worker。需要使用本版能力时创建新的 Session；存量 Session 不静默替换合同、Profile 或物理布局。
2. 默认内置配置已包含试运行授权。若部署使用自定义控制面快照（特别是 replace 模式或覆盖同名合同），需在治理流程中发布对应的机制级试运行合同与 v2 Profile，并让目标供应引用它；本次不会覆盖外部配置或升级旧快照。只调整 Excel 中的启用单元格仍不能绕过原有发布门禁。
3. 使用 Auto、同一新 Session、同一模型、渠道及材料连续发送至少两轮请求。调用方应保持顶层 `instructions` 一致，将阶段任务放在动态 `input`；若阶段系统指令确需不同，则保留其真实语义并接受前缀身份变化。本版不抽取、合并或删除指令，也不新增 Session base_instructions。
4. 首先核对计划与有效机制是否为 implicit_prefix、是否得到 finalized 绑定；再核对上述 Provider Usage 日志。conversation 模式追加历史后，完整 payload 与当前前缀摘要可能正常变化，不能要求所有轮次的完整 payload_hash 相同，也不能由材料摘要相同推定整个前缀相同。
5. 连续请求仍为 Unknown 时，需由部署方核实 AIHubMix 对所用模型的缓存能力、实际门槛及 Usage 透传；Miss 则结合内容稳定性和该渠道门槛排查。不要以内部选中了隐式机制代替真实命中验收。

## 本地验证

- 原始 6.2.0 基线：125 项既有测试通过。
- 6.3.0：125 项既有测试通过，compileall 通过。
- 临时验证覆盖：通配及精确 Gemini 供应 Auto 准入、双重授权、强制/关闭/策略拒绝、冻结备选、经济性回退、create_unknown 不回退、pending 试运行绑定完成、Native 完整 payload 等价、指纹不符在发送前拒绝、指令变化保留，以及 Hit/Miss/Unknown 的 27 种 mode/机制/Usage 组合。
- 使用 httpx 模拟上游完成 Native 解析与单次派发验证；没有真实 AIHubMix 凭据或生产数据库，因此尚未完成真实命中、生产 RPC 与费用对账验收。

## 文件范围与完整性

修改 `app/control_plane.py`、`app/cache/contracts.py`、`app/cache/intent_resolver.py`、`app/cache/orchestrator.py`、`app/cache/economics.py`、`app/cache/stateful.py`、`app/cache/usage.py`、`app/providers/gemini_native.py`、`app/core/execution_runtime.py`，以及 API/Worker 版本标识；更新 README，新增本发布说明。

原包所有文件均保留，`.git`、其他隐藏文件及 tests 内容逐文件与原包核对。原始 ZIP SHA-256：`b33192d0ef999d032c4be5c7daed4460b41feaaad452fb6680e7e6ebf03b1c20`。
