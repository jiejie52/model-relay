# Model Relay 6.6.0

基线：用户提供的 `model-relay-6.5.1-source.zip` 完整副本。发布日期：2026-11-07。

本次范围为《缓存机制升级改造方案》的要点 1、3、4：语义不变的最大合法复用、统一隐式基座与 Grok 回退、语义与链式物理指纹。API 和 Worker 版本均为 `6.6.0`。

## 设计边界

调用方表达简明意图，Relay 控制面负责有效决策，执行面运行时负责断点编排与资源绑定，协议适配层负责 Wire 投影。

调用方继续使用现有 Request/Session 接口。控制面仍以冻结的 Supply、Protocol Profile、Cache Policy 和 CacheIntentPlan 为权威；公共前缀规划器不决定路由、不扩张供应认证、不创建资源。运行时在既有租约与 fence 内准备投影、计算前缀并封存绑定；Adapter 只投影冻结的执行事实。派发后的异常处理、结果归档和原子提交沿用原流程。

## 1. 语义等价与历史冻结

- 新受理的缓存感知 Request 冻结 `cache_upgrade.version=2`。`SemanticEquivalenceNormalizer` 在 ContextPlan 之前规范化结构化 input 与输出 Schema；最终原生投影使用同一确定性序列化规则。对象键序、JSON 格式空白与合法 Schema 集合可规范化；tools 仅在冻结 Profile 声明 `unordered_tools=true` 时排序。
- 按设计的“业务字面量恒等”约束，保留自然语言、代码、业务 JSON 字符串的原始 CRLF、尾随空白和全部文字；不扫描或删除调用方正文中的时间戳、`request_id` 等字段。Relay 的追踪信息留在审计元数据，材料沿用已有的确定性来源标记，消息、角色与 Part 数组保持原顺序。
- 新增 `app/core/history_freeze.py`。每轮成功结果的 `transport_history` 以规范化原生 JSON 与 SHA-256 冻结，附加在原历史对象的 `_relay_frozen_transport` 字段；包含 reasoning、签名等不透明协议字段。历史对象仍通过原有结果归档与 Session 原子提交发布，未增加独立资源状态机。
- 下一轮受理及执行时校验历史摘要、规范化表示和 Session/连接/路由/Profile 作用域，读取冻结片段；不按本轮采样参数重新生成历史正文。原有未冻结历史按原内容读取，不回填或重写旧对象。
- ContextPlan v2 为系统指令、Schema、每个稳定材料、每轮历史和动态后缀生成独立逻辑节点与语义身份。材料元数据遵循冻结的有效材料顺序，避免读取顺序影响计划身份；不重新排列业务材料。

历史冻结会额外保存一份原生 JSON 字符串，增加历史对象大小。它保证受控原生历史片段的稳定性，不承诺不同 Provider 包装或不同思考协议一定产生相同 Wire。

## 2. 统一隐式前缀规划与 Grok 回退

新增 `app/cache/implicit_planner.py`，统一服务 Gemini Native、Responses、Chat Completions、Claude Messages 与 Moonshot Chat。各协议继续通过原 Adapter 组装材料、历史和模型参数，不各自实现 LCP 算法。

运行时在派发前对完整、规范化的原生请求计算分段链，读取同一租户、对话、Session 最近 32 个成功请求的前缀证据，在相同路由/账号/Profile/亲和 epoch 作用域中选择最长连续相同前缀。受控边界限制在完整消息或已提交完整轮次末端，不越过首个分歧、不为了复用移动内容。只读证据查询失败时记录警告，以冷前缀继续正常推理。

`CacheExecutionBinding.prefix.native_prefix_plan` 及绑定元数据引用选定边界、链摘要、估算覆盖与比较来源；完整链保存在既有 `cache_usage` 私有审计中。公开结果增加精简的 `cache.prefix_reuse`，不暴露完整内部链或 Provider 句柄。

这里的“最大”限定为当前可核实证据窗口和合法边界内的最大连续公共前缀。Relay 不掌握 Provider 缓存库存，不把本地相同前缀、估算 Token 或粘性响应头当作实际命中；`provider_hit_guaranteed=false`。真实业务指令发生变化时，分歧前的稳定前缀才可复用。短前缀、过期和上游账号状态仍由实际供应决定。

Grok 的 `RELAY_GROK_CACHE_MODE` 与 Supply 覆写保持原优先级。新受理的 v2 Request 在 `legacy_v1` 下直接调用旧隐式前缀绑定路径，不调用 `CacheOrchestrator.prepare`，不进入新版规范化和原生请求预备流程；原 Responses 缓存键、reasoning 历史和 store 行为保留。旁路保留材料绑定、活动执行租约、fence、安装绑定和 Atomic Dispatch Seal。非法 legacy 冻结合同在派发前拒绝。

环境变量修改仍需 API/Worker 进程重新加载，且只影响后续受理的 Request；已受理请求按冻结模式恢复。不承诺环境变量自动热加载或 10 秒切换。

## 3. 逐段语义/物理身份与最终实体校验

`physical_chain_v2` 为每个原生依赖、消息头和 Part 保存段类型、逐段 `semantic_hash`、`local_physical_hash`、`chained_prefix_hash`、估算 Token、累计估算、字节数、来源引用与安全边界。材料 Part 与历史轮次具有独立分类；多 Part 消息的内部节点可诊断，但不作为跨消息拆分依据。

已核实 Material 绑定的临时传输引用在语义哈希中映射到材料身份，物理哈希仍包含实际 URI/文件引用；因此可区分“内容改变”和“相同材料的 Wire 投影改变”。未核实 URL 保持字面语义，普通业务 JSON 的 `url` 字段不会被忽略。内联材料的物理表示保留完整数据，语义身份按内容摘要计算。

LCP 差异细分为 `none`、`content_changed`、`wire_projection_changed`、`length_extended`、`length_truncated`，并提供公共前缀与后缀估算。估算不代替 Provider 的 countTokens 或实际 Usage。

链的表示版本明确为 `native-block-json/2`：它是精确的规范化原生块表示，并非原始 HTTP 字节偏移或 Provider 分词链。最终完整 HTTP 实体另有独立 SHA-256：

1. Adapter 准备完整原生投影；运行时进行共享前缀规划。
2. 既有缓存编排确定最终执行绑定，Adapter 完成缓存相关投影。
3. 运行时按统一序列化计算最终实体摘要，写入执行绑定和 `prepared_payload_hash`，再安装绑定并执行原有原子派发封存。
4. Adapter 消费已准备的投影；HTTP 边界按同一规则序列化并核对摘要。实体偏差在真正发送前以 `CACHE_NATIVE_BODY_MISMATCH` 拒绝，不发送变更后的请求。

Gemini 继续保留 6.5.1 的顶层 `systemInstruction` 首字段规则。显式 `cachedContent` 与隐式机制的互斥、禁止顶层字段检查，以及 Claude 的断点与分层权限均沿用冻结合同。新的共享计划不取代这些协议约束。

## 升级与兼容

- 从已部署正常的 6.5.1 同时更新 API 与 Worker。无需新增 SQL migration、依赖或环境变量；原有 `sql/008_cache_v65_upgrade.sql` 仍是 6.5.x 缓存执行基线的前置条件。
- 保留现有 Session 的 RouteBinding、Supply/Profile 与权限。新受理的缓存感知请求进入 v2 算法；已有 Request 的 v1 ContextPlan、物理链和 LCP 算法保留，幂等重用仍优先返回原冻结解释。
- `off`、策略禁用和新 Grok `legacy_v1` 请求不启用新版隐式预备流程。旧请求不因部署升级而被补写 v2 计划或重新规划。
- 不增加模型推理调用。派发封存被拒绝时不推理；派发后的 404 等异常不自动重发。`result_stored` 恢复只提交已保存结果，不再次进入模型执行。
- 本次未调整双轨独立成本裁决、滚动快照阈值、DOCX 转换支持范围或后台保活能力；它们继续按 6.5.1 已有实现执行。

## 验证与交付

136 项离线检查通过：23 项等价/指纹/旧算法兼容检查，51 项五协议原投影对照与 HTTP 模拟检查，44 项共享运行时模拟检查，以及 18 项材料顺序、冻结完整性、嵌套媒体 URI、Gemini 参数、实体偏差拒绝、恢复和版本导入检查。Python 源码编译与 API/Worker 导入通过。

运行时模拟覆盖派发前准备、绑定安装、原子封存、单次发送、下一轮冻结历史、Grok legacy 旁路、封存/fence 拒绝、派发后错误不重发，以及可选前缀证据查询不可用。未连接真实数据库执行 RPC，也未发送生产模型或 AIHubMix 请求；真实缓存命中率、费用和数据库并发需在部署环境验收。

交付包完整保留原 ZIP 的文件、`.git` 和全部隐藏文件，未新增或修改 tests 文件。新增业务模块仅 `history_freeze.py` 与 `implicit_planner.py`；验证脚本、依赖安装目录和临时输出均不进入源码包。
