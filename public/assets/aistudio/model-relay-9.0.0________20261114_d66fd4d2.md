**MODEL RELAY**

**架构机制说明**

# 摘要：Model Relay 以统一 Session / Request、持久化调度与恢复、服务端路由、Material / Provider Binding、Provider-Neutral 能力解析、协议适配与配置治理共同构成统一模型执行控制面。

- 统一 Session / Request：同步与异步调用共用 Session、Request 快照、幂等、结果和历史提交语义；同步 Request 不进入队列，异步 Request 才创建 Job。

- **Postgres 持久化工作队列 + 按需 Worker 生命周期：** relay_jobs 仍是 durable work queue，但它从“所有调用的事实表”降为“async Request 的调度记录”；FOR UPDATE SKIP LOCKED、Lease、Heartbeat、lease_epoch/fencing、多 Worker 与 execution_pool 过滤继续负责调度、互斥与故障恢复。Worker 不以 API 容器是否 Online 作为常驻条件，而由实际请求与工作需求触发唤醒，在 active 期间持续 claim 与 drain；只有当前执行完成、当前 execution_pool 无可领取 queued Job、且不存在有效 leased/running 未完成任务后，才允许进入 quiescent 并停止主动轮询。**真正需要“跟随”的不是 Railway 面板上的 API Online/Sleeping 状态，而是 API 的实际工作需求。**

- 双层恢复 + 执行阶段恢复：调用方仍以 Checkpoint + 稳定 Idempotency-Key 覆盖业务恢复与受理回写缝隙；Relay 内部新增 request_id、request_hash、provider_dispatch_state、lease_epoch/fencing_token，并把未知 Provider 执行显式标为 indeterminate，而不是无条件重放。

- 服务端权威路由：Dify/调用方只表达 deployment/channel、provider、model 与业务参数；RouteResolver / RouteCatalog 在 Relay 内解析 connection_id，Session 创建时冻结 RouteBinding，legacy connection 字段仅作 warn/strict hint。

- **Material / Provider Binding / Provider Projection**：material_id 与 Canonical Material 保存稳定的材料身份、原始 MIME、文件名、内容 hash 与来源语义；Provider file ID、fileUri、ms://、Signed URL 以及面向 Provider 的 bridge object 都只是特定连接与授权作用域（account_scope_id）下可重建的执行表示。**材料传输通道（Material Transport）的选择由具体供应的 CapabilityContract（supported_material_transports 与切换阈值）合同化驱动**，而非全局硬编码固定阈值：支持外部公网拉取的聚合渠道可依据 Request 实际材料总字节在 External Signed URL 与 Files API 之间确定性切换，而受原生协议限制不支持任意第三方外部 HTTPS URL 直拉的官方直连渠道则按合同直接采用官方 Files API 传输，且 inference route 始终保持 Gemini Native。当 Provider 对输入 MIME 或外部对象表示存在约束时，由 Relay 在 Binding / Adapter 边界执行确定性 **Provider Projection**，而不是要求业务 Workflow 改写真实材料语义。**Transport 决定“材料通过什么受认证通道送达”，Provider Projection 决定“材料以什么 Provider-compatible 形态送达”**，二者是相互独立的执行维度。

- 原始 Error 与 Structured Output：v2 Error 通道保存完整原始实体字节、hash/base64/metadata；Structured Output 保留 canonical JSON Schema，Provider 侧只做 wire projection，模型返回后执行 canonical post-validation。

- **Provider-Neutral、五维独立身份与多模型渠道治理**：控制面、执行面、运行时与协议适配层均不按业务 stage、具体模型名或渠道名分支。系统以 **Model Catalog（模型目录）、Channel / Account（渠道与账号作用域）、Connection（端点、鉴权与凭据引用规范）、Model Supply（具体模型供应关系）、Protocol Profile（可复用协议配置）五个正交维度**刻画完整供应能力。**严格区分“协议适配器代码实现的复用”与“网关/直连级 ProtocolProfile 配置实例的解耦”**：不同渠道（如官方第一方直连与多模型聚合网关）在协议相同时 100% 复用底层 Adapter 与 Wire 投影代码，但各自绑定独立的连接规范、主机白名单、亲和头策略与执行授权证据采信模式。同一模型可以存在多个渠道供应关系并配置不同优先级；其中思考能力以声明式映射清单作为单一可信配置源（Single Source of Truth）统一定义，由控制面自动推导供应级硬支持能力摘要，在会话准入阶段与前置材料可达性校验（Material Reachability）、模态、结构化输出、安全、配额与预算共同完成资格过滤后，只有全部合格的候选才按“不可用时”切换限制与渠道顺位参与排序。单轮请求的思考控制遵循“七态标准请求意图 + 偏好/硬约束双轨策略 + 六种有效控制模式 + 三层语义（requested / effective / observed）全链路闭环”治理，使调用方业务意图与异构模型原生参数彻底解耦。

- 缓存执行控制面：**缓存不是 Provider Adapter 内的字段优化，而是统一执行控制面的一等受治理能力。**调用方只表达 requested_cache_mode=off / auto / on，不暴露 TTL、读写策略、缓存 key、资源句柄、断点位置或 Provider Native 缓存字段；Relay 在冻结的 Model Supply、Capability Contract、Protocol Profile、Cache Policy 与 ContextPlan 边界内形成 CacheIntentPlan，并在首次模型 dispatch 前形成 CacheExecutionBinding。缓存能力归属于具体 Model Supply 的有效能力集合。Relay 以**语义不变的最大合法复用**为目标，采用**通用隐式前缀基座与按需增强轨**协同的设计：**先判断条件，再生成一份统一、完整、可重放的缓存计划**，随后按照供应合同规定的组合关系、互斥约束和成本条件形成实际执行绑定。统一计划可以表达双轨组合、互斥择一、单轨或不启用缓存，具体执行不得超出冻结合同与计划的授权边界。Provider 自主透明缓存仅作为 observed fact，不改变 Relay 已选择的 effective mechanism。**缓存资源副作用与模型推理副作用必须分域。**资源创建、复用、续期、删除使用独立 operation / fencing；模型进入 dispatch_started 后，不得因缓存失效、未命中或缓存错误自动改写绑定、换机制、换渠道或“去缓存再试”。requested_cache_mode=on 若仅因本次可缓存上下文未达到门槛，应确定性降级为 effective_cache_mechanism=None 并继续同一上下文的普通模型调用；能力不存在、协议未认证或安全策略禁止等合同不满足仍按 Fail-Closed 处理。

| **语义边界：**当前实现显著收窄了重复 Provider 调用窗口：dispatch_started 后失租会进入 indeterminate，result_stored 可只恢复提交；但 Relay 仍不承诺 Provider 严格 exactly-once。Provider Files API 的中途崩溃恢复也尚未完全闭环，详见 13.2。 |
|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

| 源码依据：app/api.py；app/api_v2/router.py；app/v2_models.py；app/thinking_contracts.py；app/control_plane/\*；app/execution_plane/\*；app/runtime/\*；app/protocol_adapters/\*；app/materials/\*；app/persistence/\*；app/compatibility/\*；sql/003\_\*.sql～sql/012\_\*.sql |
|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# 目录

| **章节**                                        | **章节**                                   |
|-------------------------------------------------|--------------------------------------------|
| 1\. 文档范围与源码基线                          | 2\. 总体架构与职责边界                     |
| 3\. 统一 Session / Request 与同步、异步执行机制 | 4\. Postgres 消息队列 / Job Queue          |
| 5\. 数据持久化、Material 与 Provider Binding    | 6\. Checkpoint 主恢复 + 幂等兜底 + Fencing |
| 7\. 工作流示例：答案融合如何应用恢复机制        | 8\. 订单支付类比：稳定身份与幂等           |
| 9\. 通用穿透、路由与业务语义边界                | 10\. Provider-Neutral 与模型适配层         |
| **11. 缓存执行控制面与上下文复用**              | 12\. 安全隔离、Error 与结果边界            |
| 13\. 故障语义、可靠性边界与未完善项             | 14\. Dify / 调用方集成建议                 |
| 15\. 运维与验收建议                             | 附录 A. API / 状态速查                     |
| 附录 B. 源码追踪索引                            | 附录 C. 类比与恢复映射                     |

# 1. 文档范围与源码基线

本文沿用既有《Model Relay 技术架构与恢复机制说明》的组织方式与核心解释框架，并以当前源码作为事实基线。更新原则是：仍然有效的段落尽量保留；已经被 Session/Request、Material、RouteResolver、Native Adapter 等当前机制替代的内容，优先在原位置替换，而不是另起一套文档。

历史文档强调“Relay 把同步、易超时的模型调用转换为可持久化异步 Job”。这一判断仍适用于 async Request，但不再能代表 Relay 全部语义。当前准确基线是：Session 统一上下文和路由，Request 统一一次调用的事实身份与恢复，Job 只承担异步调度，Material 管理材料身份与 Provider 传输绑定。

| **层次**                                              | **主要源码**                                                                                                                                                                                                                           | **本文关注点**                                                                                                                                                                                                                                                                                                                    |
|-------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **API 接入层**                                        | app/api.py；app/api_v2/router.py；app/v2_models.py                                                                                                                                                                                     | **纯 HTTP 鉴权与 Owner 隔离**、入参解包、统一 RequestEnvelope 响应映射；**不包含受理编排逻辑**                                                                                                                                                                                                                                    |
| **控制面 (Control Plane)**                            | app/control_plane/\*；app/control_plane/connections.py；app/control_plane/governance/\*；app/control_plane/governance/thinking_mappings.py；app/thinking_contracts.py；app/shared_connection_contract.py                               | 统一管理、交叉校验、认证和发布五维配置及**单一信源思考映射合同**；从映射清单自动聚合推导供应级思考准入能力摘要；支持外部声明式配置直接注册新连接与供应，离线编译并输出深度只读的 ImmutableConfigSnapshot                                                                                                                          |
| **执行面 (Execution Plane)**                          | app/execution_plane/\*；app/execution_plane/thinking_resolver.py；app/execution_plane/admission.py；app/execution_plane/material_reachability.py；app/execution_plane/schedulers/\*                                                    | 会话准入与跨渠道材料可达性前置过滤、供应选路与连接身份冻结、**七态思考意图与偏好/硬约束双轨策略解析、思考激活态与采样参数互斥校验**、冻结 FrozenExecutionPlan、同步/异步调度、**含思考观测事实门禁的只提交恢复**、Canonical Schema 后置校验、双模执行授权与思考合同后置核验及原子提交                                             |
| **运行时 (Runtime)**                                  | app/runtime/\*；app/runtime/connection_scope.py；app/runtime/resource_adapters/\*                                                                                                                                                      | 核验活跃 ExecutionFence 与冻结连接实例指纹，按合同落实渠道差异化的材料传输（External URL / Files API）与显式缓存物化（inline_bytes_only / file_uri_allowed）、条件化会话亲和、发送前原子封存（AtomicDispatchSeal）与受控调用                                                                                                      |
| **协议适配层与统一工厂 (Protocol Adapter & Factory)** | app/protocol_adapters/\*；app/protocol_adapters/thinking_projection.py；app/protocol_adapters/thinking_evidence.py；app/protocol_adapters/factory.py；app/protocol_adapters/native_evidence.py；app/control_plane/resource_registry.py | 公共工厂按 connection_id 统一装配并隔离推理、文件、显式缓存、Token 计数及 SDK 客户端，强制注入 TransportGuard 与多渠道脱敏 http_wire_logger；只读消费 SealedDispatchBundle 及**已冻结的有效思考语义执行注册式确定性 Native Wire 投影**，双模提取网关响应头或原生响应体证据（含上游思考观测证据）并输出 StandardizedProviderResult |
| **材料与持久化基座**                                  | app/materials/\*；app/persistence/\*；app/core/\*；sql/003\_\*.sql～sql/011\_\*.sql                                                                                                                                                    | Canonical Material 入库、回退字节与桥接产物存储、Provider 原生文件适配、Raw Error 保真归档、Postgres 状态机与**双 Fencing RPC**                                                                                                                                                                                                   |
| **Legacy Compatibility & 传输门面**                   | app/compatibility/dify_v2.py；app/compatibility/relay_gateway.py；app/compatibility/legacy_worker.py；app/compatibility/fusion_runtime.py                                                                                              | 旧 /v1 Job / Fusion 运行时兼容隔离区，以及通过服务端受控 ASGI 作用域注入可信入口审计、无损桥接主执行链路的**显式传输门面**                                                                                                                                                                                                        |

## 1.1 从旧基线到当前实现的关键替换关系

| **旧基线**                               | **当前事实**                                                      | **影响**                                                |
|------------------------------------------|-------------------------------------------------------------------|---------------------------------------------------------|
| Job 是一次调用的主身份                   | Request 是主事实身份；Job 仅 async                                | 恢复优先查询 request_id；同步调用也可持久化、去重和重读 |
| 调用方可传 connection                    | connection 由 RouteResolver 解析并在 Session 冻结                 | Dify 不再拥有 Adapter/Wire 选择权                       |
| 材料主要等同于 Storage 对象/Signed URL   | material_id、fallback object、Provider binding 分离               | 传输资源可过期/重签/重绑，不改变材料身份                |
| Fusion Runtime 可由 Worker 按 stage 分支 | v2 Core 不按 Fusion stage 分支；legacy compatibility 保留旧路径   | 业务语义继续留在应用层                                  |
| 错误摘要为调用方主接口                   | v2 raw Error 保真；完整原始实体可读取                             | 诊断不依赖统一 UPSTREAM\_\* 摘要                        |
| Lease 过期可再次执行 Job                 | dispatch_started 失租 → indeterminate；result_stored → 只恢复提交 | 失租不再被视为“Provider 一定未执行”                     |

| **验证口径：**本次对当前源码执行了 compileall、API/Worker import 与 pytest；在提供必需的本地占位环境变量后，68 项测试全部通过。该结果证明代码与单元/边界测试一致，不等同于对 AIHubMix、Gemini、Moonshot、Supabase 的真实生产网络链路重新做端到端验收。 |
|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

| **不做的事情：**本文不会把源码未实现的能力包装成既成事实，例如 Kafka/RabbitMQ、优先级队列、Dead Letter Queue、严格 Provider exactly-once、健康状态驱动的动态多账户路由、原子批量 Material Ingress、完整流式输入处理或完全闭环的 Provider File upload crash recovery。 |
|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# 2. 总体架构与职责边界

<img src="model-relay-media/media/image1.png" style="width:6.6in;height:3.46447in" />

图 1 Model Relay 总体架构：调用方业务意图、服务端 RouteBinding、统一 Request 与可选 Job

Relay 的关键解耦是：调用方负责“为什么要执行、业务推进到哪里、模型结果是否满足业务 Gate”，并以 input、instructions、**think_level、think_level_policy**、options、structured_output、execution、metadata 以及 requested_cache_mode 表达模型调用意图；Relay 负责把调用受理为稳定 Request，冻结 RouteBinding 与能力合同，**依据单一思考映射清单与能力合同将调用方请求意图（requested）解析为确定性的有效执行语义（effective）**，形成上下文与缓存逻辑计划，准备材料与物理缓存绑定，执行或排队，采集上游观测证据（observed），保存原始结果/错误并提供恢复。业务 Workflow 不维护 Provider Wire，**不维护各模型异构的思考档位白名单、Token 预算映射或推理开关结构**，也不维护缓存 TTL、缓存 key、Provider 资源句柄、cache_control、cachedContent 或模型/渠道专属分支。

**调用方表达简明业务意图，Relay 内部严格遵循控制面、执行面、运行时、协议适配层四层职责分离，各层之间通过不可变契约单向推进，严禁跨层回流或职责越界**：

| **层次**       | **主要职责**                                                             | **主要产出（标准契约）**                                                        |
|----------------|--------------------------------------------------------------------------|---------------------------------------------------------------------------------|
| **控制面**     | 管理、认证和发布供应、能力、协议及策略                                   | **不可变配置快照 (ImmutableConfigSnapshot)**                                    |
| **执行面**     | 受理请求、解析有效语义、冻结逻辑计划、同步 / 异步调度、恢复与结果提交    | **Request、ContextPlan、CacheIntentPlan**（封装为 **FrozenExecutionPlan**）     |
| **运行时**     | 核验执行条件，落实材料表示、缓存资源与物理绑定，完成发送前封存和受控调用 | **Material Binding、CacheExecutionBinding、实际执行事实 (ActualExecutionFact)** |
| **协议适配层** | 按已冻结事实构建原生请求，解析原生响应与用量                             | **Native Wire、标准化 Provider 结果 (StandardizedProviderResult)**              |

四层端到端协同链路固定如下：

- **控制面**：Model Catalog → Channel / Account → Model Supply → Capability Contract ↘ Protocol Profile → Cache Policy → Route Policy → **不可变配置快照 (ImmutableConfigSnapshot)**。

- **执行面（受理与规划段）**：调用意图 → 供应资格过滤与路由冻结 (**RouteBinding**) → 有效语义解析 (**EffectiveSemantics**) → 受控等价规范化与上下文规划 (**ContextPlan**) → 缓存条件筛选与统一规划 (**CacheIntentPlan**) → 持久化 **Request 快照**并进入同步/异步调度。

- **运行时**：活跃执行权核验 (**ExecutionFence**) → 材料传输与投影绑定 (**Material Binding**) → 物理上下文构建与缓存只读评估 → 集中兼容性与成本仲裁 → 选定分支物化 (**CacheExecutionBinding**) → **发送前原子封存 (AtomicDispatchSeal)** → 单次受控调用 → 原始响应/错误落盘并产出**实际执行事实 (ActualExecutionFact)**。

- **协议适配层**：消费封存包 (**SealedDispatchBundle**) → Schema 与缓存 **Native Wire** 投影 → 传输围栏核验与 HTTP 报文观测 → Provider 交互 → 原生响应解析与用量归一化 (**StandardizedProviderResult**)。

- **执行面（校验与提交段）**：消费**实际执行事实**（或 **result_stored** 恢复事实） → 上游执行授权核验与 **Canonical Schema 后置校验** → 生成 compact_result → **Session 历史与 Request/Job 终态原子提交**。

**逻辑计划在执行面先冻结，物理绑定在运行时后落实；只读集中仲裁先完成，缓存资源副作用后发生。发送前原子封存构成对协议适配层执行输入的原子授权与完整性记录；原始成功实体在运行时先归档，业务合同后置校验与事务提交在执行面最后收口。**

**模型、渠道、协议三者必须保持独立治理：模型回答“调用谁”，渠道回答“从哪里获得服务”，协议回答“如何表达与解析调用”。**

| **组件**                                                        | **职责**                                                                                                                                                                                                                                                                                                                                                                 | **不承担的职责**                                                                                                                                                                       |
|-----------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **控制面 (SnapshotCompiler / RouteCatalog)**                    | 编译、交叉校验、认证并发布不可变配置快照；维护五维配置（Model Catalog、Channel / Account、ConnectionSpec、Model Supply、CapabilityContract、ProtocolProfile）及 CachePolicy / RoutePolicy；支持外部声明式配置直接登记新连接与供应，\_builtin() 仅作可选兼容种子；输出 snapshot_id、catalog_hash 与深度只读 ImmutableConfigSnapshot                                       | **不介入单次请求热路径**；不访问在途会话；不创建材料或缓存资源；不允许原位修改已发布快照                                                                                               |
| **执行面 (SessionManager / RequestAcceptor / ResultCommitter)** | 消费不可变配置快照完成会话准入、跨渠道 Material Reachability 前置过滤与供应选路 (RouteResolver)；冻结完整连接身份（account_scope_id、connection_spec_hash）及 FrozenExecutionPlan；解析 requested/effective 有效语义；编排 Inline / Queue 调度与 result_stored 只提交恢复；执行网关响应头/第一方原生响应体双模上游授权核验、Canonical Schema 后置校验与 Session 原子提交 | 不直接操作 Provider Files API 或生成 Signed URL；**不读取实时缓存库存或动态复用率来改变逻辑请求身份**；不拼装 Native Wire                                                              |
| **运行时 (RelayRuntimeEngine)**                                 | 在每个物理阶段边界与底层发包前核验活跃 ExecutionFence 及冻结连接实例指纹；落实合同驱动的 Material Transport / Material Binding、物理上下文与 CacheExecutionBinding；执行条件化 Session Affinity、AtomicDispatchSeal 与单次受控调用；落盘原始响应/错误并归档 ActualExecutionFact                                                                                          | 不修改执行面已冻结的 RouteBinding、ContextPlan 或 CacheIntentPlan；不承担 Canonical Schema 最终业务判定或 Session 历史推进；**进入 dispatch_started 后绝不换机制、换渠道或去缓存重发** |
| **协议适配层与统一工厂 (Protocol Adapter & Factory)**           | 公共工厂按 connection_id 统一装配并隔离推理、文件、显式缓存、Token 计数及 SDK 客户端，强制注入 TransportGuard 与 http_wire_logger；Adapter 只读消费 SealedDispatchBundle，构建 Native Wire，提取 gateway_headers 或 first_party_native_payload 证据并输出 StandardizedProviderResult                                                                                     | 不感知 requested_cache_mode 或路由策略；不在投影时改读未冻结的最新材料字节；不在适配器或 SDK 封装层私自创建裸 HTTP 客户端；不因上游报错自行删减缓存字段重发                            |

| **源码依据**：app/api.py；app/api_v2/router.py；**app/control_plane/**\*；**app/execution_plane/**\*；**app/runtime/**\*；**app/protocol_adapters/**\*；app/worker.py；sql/003\_\*.sql～sql/011\_\*.sql |
|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# 3. 统一 Session / Request 与同步、异步执行机制

<img src="model-relay-media/media/image2.png" style="width:6.6in;height:3.90219in" />

图 2 sync/async 共用 Request 受理：同步不建 Job，异步同事务创建 Job

## 3.1 Session 创建：先解析并冻结 RouteBinding

POST /v2/sessions 以 tenant_id、conversation_hash、provider、model、context_policy、material_ids 等业务意图创建 Session，并以稳定 Idempotency-Key 保证同一会话意图只形成一个 Session；同键但会改变执行边界的意图不一致时拒绝复用。

Session 创建阶段由服务端根据 model intent 在动态聚合的受管供应组（Supply Group）内枚举可用的 ModelSupply 候选。候选必须先通过 deployment / tenant / region、连接凭据就绪度与安全策略、跨渠道材料可达性前置校验（material_reachability：核验材料在目标供应的 account_scope_id 与 supported_material_transports 下是否已有就绪绑定，或具备 relay_backed / source_recoverable 字节可供重建）、CapabilityContract（其中**会话级思考能力资格直接对齐编译器从「思考映射清单」自动聚合推导出的硬约束支持集合**，无需在模型清单中重复维护静态档位列）、ProtocolProfile、配额、预算与健康状态等资格检查。对于延迟到首轮请求具象化供应的受管供应组，执行面同样依据首轮请求的实际思考意图、约束策略（think_level_policy）及结构化输出要求对候选供应做前置能力校验。只有全部合格的候选才进入该模型的渠道优先级（priority）排序，并严格服从渠道行的“不可用时（On Unavailable）”向下切换限制；优先级只用于合格供应之间的确定性选择，绝不能把材料不可达或不满足思考及调用合同的渠道重新变成可选。

选择完成后，Session 永久冻结具体 ModelSupply 及其完整连接执行身份，包括 provider / model、channel、**授权作用域标识（account_scope_id）**、connection_id、**连接规范不可变摘要（connection_spec_hash）**、execution_pool、ProtocolProfile、CapabilityContract、RoutePolicy、CachePolicy、RouteBinding 及其不可变快照身份。**account_scope_id 与 connection_spec_hash 作为强类型契约字段贯穿 FrozenExecutionPlan 与运行时 SealedDispatchBundle**：前者定义跨请求的文件与缓存资源物理隔离边界，后者确保运行时从 ResourceRegistry 获取的客户端实例与冻结合同严格一致；当端点、账号或协议发生变化时必须形成新的配置身份，严禁既有会话或恢复中的请求静默漂移至新连接。

Session 冻结的是缓存可执行合同，不是某次 Request 的 effective_cache_mechanism，也不是一个可被反复覆盖的 Provider cache_id。

后续 Request 只能在该冻结供应关系内解析缓存意图。缓存资源过期、冷热变化、命中情况或策略更新只影响允许窗口内的物理准备事实，不授权重新选路。缓存能力属于该具体 Supply 的有效能力交集，不由模型名称直接推导。

Session 只冻结缓存能力与策略合同；Request 的缓存适用性、Fail-Closed / Fail-Safe 及门槛处理详见第 11.2、11.4 节。

## 3.2 Request 受理：Request 是每轮调用的稳定事实身份

Request 受理前先装载 Session 冻结的 Model Supply、Protocol Profile、Capability Contract 与 Cache Policy，并依据本轮 Request 的 **context_policy** 确定模型历史可见性（**model_history_visible**）与 Session 锚定材料可见性。

执行面纯函数材料位置规划器将会话材料拆分为会话自动附加材料（**session_manifest_auto**）、调用方当轮显式提交材料（**request_explicit**）与已提交原生历史材料发生（**history_committed**）三态来源语义，产出不可变的唯一材料位置权威契约（EffectiveMaterialPlacementPlan）。

在本轮历史对模型可见，且 ProtocolProfile 声明支持原生历史材料复用的前提下，Relay 才允许依据已提交历史中经过完整性验证的材料发生凭证，将已存在于可信历史中的 Session 自动附加材料收敛为历史继承状态（**historical_only**），避免重复生成当轮附件 Part。

对于调用方在本轮请求体中显式重提的同源材料，仍完整保留其当轮投影位置，并抑制同轮自动候选的额外叠加。

**当本轮历史对模型不可见时，历史中已经存在的材料发生不能作为模型可见上下文的复用依据。** 此时：

- **session_materials_only** 必须将 Session manifest 中的材料重新纳入本轮有效材料投影。

- **explicit** 不自动纳入 Session manifest，仅保留本轮显式提交的材料。

- 两种模式均正常保留本轮 **material_ids** 所指定的材料。

无论历史是否对模型可见，完整已提交历史及材料发生账本仍须用于内部版本、完整性与资源生命周期校验，不得因为本轮上下文投影而被删除、覆盖或改写。

**材料去重以本轮模型实际可见的上下文为依据，而不是简单依据材料是否曾经出现在 Session 历史中。**

ContextPlan 由本轮有效指令、模型可见历史、有效材料身份及其必要的表示规划事实构成，不依赖尚未生成的最终 Provider Material Binding 或 Provider cache handle。Relay 始终以“模型能力 ∩ 渠道供应能力 ∩ 协议与运行时能力 ∩ 安全/租户策略”为可执行能力边界解析模型调用与缓存候选。

**Request 级上下文可见范围控制（Request Context Policy）。**

Session 创建时的 **context_policy** 仍然作为会话级默认上下文策略，并在 Session 生命周期内保持冻结。与此同时，Request 允许通过同名可选字段 **context_policy** 对本轮模型可见上下文进行临时覆盖。

**Request 级上下文策略只控制本轮模型能够看到什么，不改变 Session 原有的历史管理、持久化及恢复机制。**

具体支持以下三种上下文视图：

| **Request context_policy** | **已提交历史消息**              | **Session 锚定材料** | **本轮显式材料** |
|----------------------------|---------------------------------|----------------------|------------------|
| **省略**                   | 继承 Session 默认策略           | 按 Session 默认策略  | 保留             |
| **conversation**           | 满足 Session 历史累计条件时继承 | 保留                 | 保留             |
| **explicit**               | 不继承                          | 不自动加载           | 保留             |
| **session_materials_only** | 不继承                          | 保留                 | 保留             |

其中：

- **conversation**：在 Session 默认策略为 **conversation** 且 **history_mode=cumulative** 时，向模型提供完整的已提交历史及 Session 锚定材料；若 Session 使用 **ephemeral_turn**，则不继承历史，但仍可使用 Session 锚定材料。

- **explicit**：本轮只使用调用方显式提供的输入、指令和材料，不自动继承历史消息及 Session 锚定材料。

- **session_materials_only**：保留 Session 固定锚定材料及本轮显式材料，但不向模型提供此前的对话历史。

Session 创建时采用 **context_policy=explicit** 的会话，不允许通过 Request 级覆盖提升为 **conversation** 或 **session_materials_only**，避免突破原有会话上下文授权边界。

**Request 级覆盖不改变 Session 的历史提交规则。** 对于采用 **conversation + cumulative** 的 Session，即使某一轮使用 **explicit** 或 **session_materials_only**，该轮成功后仍然正常追加到 Session 完整历史。后续 Request 不指定覆盖策略时，仍可恢复原有累计历史继承行为。

完整历史仍然参与 **expected_history_version**、历史完整性、材料发生账本及恢复一致性校验，但只有经过本轮上下文策略选定的历史内容才进入 Provider Wire。

Request 显式传入的 **context_policy** 及其对应的模型历史可见性结果必须纳入请求身份冻结与幂等校验。相同 Idempotency-Key 下改变上下文策略，应视为不同执行意图并拒绝复用；未传入该字段时保持原有请求身份兼容行为。

需要明确的是，以上机制属于**固定上下文范围选择，而非历史内容智能筛选**。Relay 不会自动从被隐藏的历史中提取用户裁决、融合方案或历史建议，也不会对当前输入和单份 Material 内部进行语义过滤。完成任务所必需的信息仍由调用方通过本轮输入、指令及显式材料提供。

本轮缓存请求的 off / auto / on 意图与适用性决策由缓存专项控制面处理，具体规则见第 11.4 节。

在**执行面**的受理流水线（**RequestAcceptor**）中，缓存意图解析先依据冻结合同、协议规则、缓存策略与 **ContextPlan**，确定各机制的适用资格、排除原因及待核验条件；随后由统一规划器（**CacheIntentPlanner**）生成一份完整、可重放的 **CacheIntentPlan**。**执行面只冻结候选资格、条件、策略与授权降级分支，绝不读取当前 ready 缓存库存或近期动态复用统计来改变逻辑请求身份；实时复用率、TTL 预测与动态经济性证据严格留到运行时仲裁阶段评估，只写入运行时计划副本与执行绑定，不回写已冻结的逻辑计划。**

Request 受理入口首先利用请求体字段显式存在性检查与服务端受控 ASGI 作用域，精确捕获思考参数的**原始输入来源状态（区分未传参省略 omitted、显式空值 explicit_empty 与调用方显式传值 caller_explicit）以及不可由客户端 Header 伪造的可信调用入口审计信息**。随后，语义解析器依据冻结能力合同将调用方输入归一化为七态标准意图，并结合 think_level_policy（prefer 或 require）生成结构化的 thinking 快照节点，完整冻结 input、instructions、materials、Model Supply / RouteBinding、CapabilityContract、ProtocolProfile、CachePolicy、requested / effective 模型与思考语义、requested_cache_mode、ContextPlan 摘要、CacheIntentPlan 摘要、history identity、Material manifest、structured_output、metadata 与 execution。

request_hash 严格采用冻结能力合同解析出的**有效执行语义（EffectiveSemantics）与硬约束验收边界**参与身份计算：在思考维度上，**唯有决定实际物理调用与后置验收边界的有效语义（有效思考模式 mode、原生投影动作 wire_action、投影策略 wire_strategy、有效档位 level、有效预算 budget_tokens）以及约束策略 policy 纳入请求身份计算**；而原始请求标签、字段来源状态、映射规则标识、映射原因说明及可信入口审计记录仅保存在 request.json 与审计元数据中，绝不污染逻辑身份。因此，对于固定开启思考（fixed_on）的模型、配置映射到相同 Token 预算的档位、或在普通偏好（prefer）策略下适配收敛至同一有效执行状态的请求，调用方传入的不同思考标签自然收敛为同一逻辑 Request 身份；而显式缓存 on / off、约束策略 prefer / require、输入正文、材料顺序与执行方式仍严格区分。已持久化的历史 Request 保持其受理时的原始身份算法与冻结快照，不因后续契约演进而重新解释。临时资源句柄、缓存库存、实际命中、上游思考观测结果（observed_thinking）、壁钟时间和实际用量不进入逻辑 Request 身份。同时，为确保 PREPARED 缝隙重放与 result_stored 同步只提交恢复零冲突，request_hash 在材料维度严格采用受理时冻结的 **（位置合同版本、会话材料清单摘要、当轮显式材料序列、预期历史版本）四元组** 参与幂等身份计算；对已存在的请求重放优先依据其原冻结快照校验，绝不因后续历史推进或内部灰度切换产生误判。

**执行面在受理阶段冻结逻辑计划（FrozenExecutionPlan），运行时在首次受控调用前落实物理绑定并完成发送前原子封存（SealedDispatchBundle）**；两级冻结和执行门槛详见第 11.5～11.7 节。同一 Session + Idempotency-Key 已存在时，若 request_hash 相同直接返回原 Request；若不同则 409。新 Request 的 request.json 先写入 Object Storage，随后 **accept_relay_request** 在单个数据库事务里锁定 Session、复核幂等、检查 active_request_id 并创建 relay_requests（同步模式同时授予 Request 级初始执行租约，async 模式在同一事务里创建唯一关联的 relay_jobs）。

## **3.3 同步路径：执行面内联调度、运行时受控执行与只提交恢复围栏**

sync Request 由 API 进程内的**执行面同步调度器（InlineScheduler）**驱动：调度器同时启动执行子任务与租约心跳任务，先调用**运行时引擎（RelayRuntimeEngine）**完成条件核验、材料与缓存物理绑定、发送前封存及单次受控调用，拿到运行时落盘的**实际执行事实（ActualExecutionFact）**后，再交由**执行面结果提交器（ResultCommitter）**完成 Canonical 合同校验与 Session 原子提交。**因此“同步”不等于“无状态直连 Provider”，它完整历经执行面、运行时与协议适配层的受控链路。**

sync_request_deadline_seconds 默认 120 秒（与底层执行租约保持独立安全余量）。**若等待超时或心跳续租失败，调度器立即永久标记当前执行权失效、取消并等待执行子任务退出，绝不把同一请求改造成新的 async Job**：

- **若在 dispatch_started 之前失租或中断**：请求保持未发送状态，不触发上游调用；

- **若在 dispatch_started 之后超时或连接中断**：权威恢复将其标为 **indeterminate** 并保留 Session 占用与资源 pin；

- **若运行时已将成功响应归档为 result_stored、仅在最终提交前中断**：则在执行子任务退出后释放同步**只提交恢复围栏（commit-only fence）**，允许调用方以原 Idempotency-Key 重放相同 POST 直接在**执行面**完成只提交恢复，**不创建新 Request、也不再次进入运行时或调用模型**（GET 查询始终保持纯只读）。

## 3.4 异步路径：Job 只承担调度

async Request 在受理事务中同时获得 request_id 与 job_id，Request 与 Job 的 durable 事实仍由数据库事务独立成立。API 的实际请求活动同时作为 Worker availability 的控制信号，通过部署私网触发按需 wake；因此即使 Worker 已处于 Sleeping，新的工作需求也可以先唤醒消费者，再由 Worker 进入正常的 claim_relay_job_v2 路径。**Wake 只解决“消费者何时在线”，不参与 Request/Job 事务，也不改变 request_id、job_id、Lease、lease_epoch 或 provider_dispatch_state 的任何恢复语义。** Worker 被唤醒后领取允许 execution_pool 内的 Job，由**执行面队列调度器（QueueScheduler）**持有 Lease / Heartbeat / lease_epoch 执行：若 Request 处于 **not_sent**，调度器装载 **FrozenExecutionPlan** 调用**运行时（RelayRuntimeEngine）**并在获得 **ActualExecutionFact** 后交由 **ResultCommitter** 提交；**若 Request 已处于 result_stored，调度器直接在执行面调用 ResultCommitter 读取已归档执行事实完成校验与原子提交，完全旁路运行时与协议适配层**。Job 的成功、失败、取消或 indeterminate 状态继续与关联 Request 原子同步更新。

## 3.5 统一 Envelope 与 Compact Result

sync 与 async 共用统一 RequestEnvelope：session_id、request_id、可选 job_id、execution.mode、status、history_version、result、error。模型原始成功实体与 response.output 写入 Object Storage；调用方只把 result 中的业务输出作为 Parser/Schema 输入，usage、request_id、response_id、cached_tokens、object_id、status 等 transport/provider metadata 不得混入业务 Schema。若可见文本超过 soft limit，会保存完整文本对象并在 compact result 中返回可恢复引用；compact 表示继续受 hard limit 约束。调用方解包必须面向结构，不得把 schema_version 的精确值作为读取 result.text 或 compact business result 的前置条件；协议演进时只要稳定业务结构仍成立，就不应因 schema_version 值变化把整个 Relay result 对象误当成业务 JSON。

## 3.6 缓存执行的接口边界

缓存沿用统一 Session / Request 的身份与恢复边界。Session 保存冻结的供应与可执行合同，每轮 Request 保存缓存意图及逻辑计划，执行阶段形成物理绑定。**缓存启用与否只能改变上下文的复用方式，不能改变本轮调用的业务上下文。**

缓存机制、适用性判断、物理绑定、资源生命周期与观测规则统一见第 11 章。

| **源码依据**：app/api_v2/router.py；**app/execution_plane/session_manager.py**；**app/execution_plane/request_acceptor.py**；**app/execution_plane/result_committer.py**；**app/execution_plane/schedulers/**\*；**app/runtime/engine.py**；sql/003\_\*.sql；sql/010_execution_plane_commit_recovery.sql；sql/011_execution_authority_closure.sql |
|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# 4. Postgres 消息队列 / Job Queue

<img src="model-relay-media/media/image3.png" style="width:6.6in;height:3.70307in" />

图 3 async Job 状态与 provider_dispatch_state：Lease 过期不等同于“上游未执行”

从消息队列视角看，relay_jobs 仍然承担“async 调度队列 + Job 状态事实表”。区别在于当前实现中 relay_requests 才是一次模型调用的事实表，Job 通过 request_id 与 Request 一一关联。旧 v1 Job 没有 request_id，Worker 会进入 compatibility 执行路径。

| **队列能力**  | **当前实现**                                                                                                                                                           |
|---------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| 生产者入队    | accept_relay_request 仅在 execution.mode=async 时创建 relay_jobs；Request 与 Job 同事务受理                                                                            |
| 消费          | Worker 按需唤醒；active 期间主动轮询 claim_relay_job_v2 并持续 drain 当前 execution_pool，满足安全 quiescent 条件后停止 DB/provider polling；新的工作需求再次触发 wake |
| 执行范围      | 按 worker_execution_pools / execution_pool 过滤；支持 Railway/SAE 同源码分工部署                                                                                       |
| 并发抢占      | FOR UPDATE ... SKIP LOCKED；同一时刻不 claim 同一行                                                                                                                    |
| Lease         | 领取后 lease_owner + lease_expires_at + lease_epoch；默认 120 秒                                                                                                       |
| Heartbeat     | Worker 默认每 30 秒 renew_relay_job_lease_v2，并要求 owner + epoch 匹配                                                                                                |
| 安全重领      | 过期且 Request provider_dispatch_state 为 not_sent 或 result_stored 才允许重领                                                                                         |
| 未知执行      | 过期且 provider_dispatch_state=dispatch_started → Request/Job 转 indeterminate，不重发 Provider                                                                        |
| 结果恢复      | result_stored 被重领时 QueueExecutor 直接 complete_request，不重新推理                                                                                                 |
| 排序/扩展边界 | created_at ASC；没有源码级优先级、DLQ、Topic/Fan-out                                                                                                                   |

## 4.1 为什么它可以称为“消息队列机制”

它具备异步入队、持久化、消费者领取、并发互斥、消费租约、故障重领、执行范围过滤、状态查询与 fencing，因此在任务分发语义上构成 durable work queue。Broker 仍由 Postgres 表和 RPC 替代。

## 4.2 为什么它又不是 Kafka/RabbitMQ

- 没有 Topic / Exchange / Consumer Group 等 Broker 抽象；主要模型仍是“一条 async Job 由一个 Worker 领取”。

- 没有优先级 claim、延迟队列或 Dead Letter Queue；失败/未知执行直接体现在 Request/Job 状态。

- Worker 只在 active 状态下主动轮询数据库，当前单实例仍串行处理已 claim Job，横向并发仍依赖多个 Worker Replica。**按需唤醒与休眠只改变消费者的空闲生命周期，不改变 Postgres durable queue 的 claim、Lease、Heartbeat、Fencing 与恢复语义。**

- 当前工作负载仍是高成本、低频率、长耗时模型任务，Postgres 的事务、审计、恢复与 Session 原子提交比专用 Broker 的吞吐优势更重要。

## 4.3 Worker 按需唤醒、Drain 与休眠边界

Worker 生命周期属于部署执行控制面，不属于 Request/Job 业务状态机。API 的实际请求活动通过同项目私网向 Worker 产生 wake 信号：Worker 已 Sleeping 时，该流量负责重新唤醒服务；Worker 已处于 active 时，则只延续当前工作窗口。控制链路采用 request-driven wake，而不是用持续生命周期 heartbeat 把 API 与 Worker 强绑定；Request、Job、Lease 与恢复事实仍完全由 Postgres 和既有 RPC 保证。**API Online + Worker Sleeping 可以是正常的空闲稳态**，因为 API 进程是否存活与当前是否存在需要 Worker 消费的异步工作是两个不同维度。

当 API 请求活动消退后，Worker 不能直接停止。Worker 必须先进入 drain 判定，并同时满足以下条件：当前进程没有正在执行的 Job；当前 execution_pool 没有可领取的 queued Job；当前 execution_pool 不存在仍然有效的 leased/running 非终态 Job；旧 Worker 实例遗留下来的有效 Lease 也必须被视为未完成工作，等待 Lease 到期并进入既有恢复路径。对过期 Lease，仍严格依据 provider_dispatch_state 处理：not_sent 可以安全重领，result_stored 只恢复 complete_request，dispatch_started 保守进入 indeterminate。**“当前没有立即 claim 到 Job”绝不等于“所有任务已经处理完毕”。**

只有上述 drain gate 全部满足，Worker 才允许停止 DB/provider 主动轮询并进入 quiescent。quiescent 表示应用已经具备休眠条件，不等于 Railway 面板必须立即显示 Sleeping；最终休眠由 Serverless 平台根据后续网络 inactivity 决定。新的 API 工作需求到达后重新触发 wake，Worker 回到 active 并继续正常 claim。

Worker 生命周期控制采用可用性优先的 Fail-Safe。若控制服务异常、控制链路不可用，或 Worker 无法确认自己能够被可靠重新唤醒，则不进入不可恢复的休眠路径，而保持或回退为原有持续 polling。**资源节省不能以“Worker 已经睡眠，但新的 durable Job 无法获得消费者”为代价。**

| **源码依据：**sql/003_relay_v2_session_request_material.sql::claim_relay_job_v2/renew_relay_job_lease_v2；app/v2_repository.py::claim_job_v2；app/worker.py::run_forever/\_heartbeat；app/execution/queue_executor.py |
|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# 5. 数据持久化、Material 与 Provider Binding

原文“控制信息进 Postgres，大对象进 Storage”的原则继续成立，但对象类型已经扩展。当前实现把 Session、Request、Job、Material、Provider Binding、执行尝试与对象定位分别建模；Supabase 仍是当前默认 Object Storage，但输入文件字节只在策略或 transport 需要时进入 fallback/bridge，不能再把“Material = Supabase object”视为总模型。

| **存储层/表**                  | **典型数据**                                                                                                                             | **意义**                                              |
|--------------------------------|------------------------------------------------------------------------------------------------------------------------------------------|-------------------------------------------------------|
| relay_sessions                 | owner、provider/model、connection、context_policy、material_manifest、history_version、active_request_id、execution_pool、route metadata | 会话上下文与冻结执行身份                              |
| relay_requests                 | request_hash、snapshot object、execution mode/status、provider_dispatch_state、binding snapshot、provisional/final result/error          | 一次模型调用与恢复的权威事实表                        |
| relay_jobs                     | request_id、execution_pool、Lease/Heartbeat/lease_epoch、compact_result                                                                  | async Request 的调度记录                              |
| relay_materials                | material_id、filename/MIME、actual_size、sha256、durability、target route、binding generation                                            | 稳定材料身份和材料事实                                |
| provider_material_bindings     | connection/account scope、representation、external_file_id/uri、generation、expires/state                                                | Provider 侧可消费传输资源                             |
| material_fallback_objects      | storage_id/bucket/key/hash/size/retention                                                                                                | 必要时保存原始输入字节，用于 bridge/durability/rebind |
| material_binding_attempts      | attempt_id、phase、provider request id、raw response/error ref、uncertain                                                                | Provider File upload 尝试审计；恢复闭环仍不完整       |
| relay_objects + Object Storage | request snapshot、raw response、response.output、history、raw error、Kimi extraction、fallback bytes                                     | 大对象定位与内容 hash；读取按 storage_id 解析         |
| legacy fusion\_\*              | Corpus / Artifact 等 legacy Fusion 持久化                                                                                                | 仅兼容旧 Fusion 运行时，不属于 v2 Core 主模型         |

## 5.1 Material 身份、Fallback 与 Provider Binding 三者不能混为一谈

material_id 是调用方长期保存的稳定身份；Relay Object / fallback object 表示 Relay 当前是否持有原始字节；Provider Binding 表示特定 connection/account scope 下的 file ID、URI、Signed URL 或 extraction artifact。Provider 资源过期、重签或重绑时，material_id 不应变化。

**Provider Projection 是 Provider Binding 边界上的衍生表示，不是新的 Material，也不是对 Canonical Material 的改写。** Canonical Material 始终保存调用方提交材料的真实 filename、content_type、内容 hash 与来源语义；Relay 可以针对特定 Provider / transport，从相同材料字节构造 Provider-compatible bridge object，并将其 MIME、文件表示及 URI 冻结到对应 Binding 中。例如，文本语义的 JSON Artifact 在 Canonical Material 中仍保持 application/json 与 .json，但当 Gemini External URL 输入要求兼容的文本表示时，可投影为 text/plain 与 .txt 的 bridge object；PDF 等已具备合法 Provider 输入表示的材料则继续保持 application/pdf 与原文件名。**业务事实保持真实，Provider 表示允许确定性投影；投影变化不得反向污染 Material 身份与业务语义。**

多渠道场景下，同一个 Canonical Material 可以同时存在多个 Provider Binding，但每个 Binding 都必须绑定到明确的 Model Supply / channel / connection / account scope，Provider file ID、fileUri、Signed URL、extraction artifact 等资源不得跨渠道或跨账号作用域直接复用。路由候选资格检查必须把材料可达性与 Provider Projection 能力纳入约束：若候选供应缺少可用 Binding，但 Relay 持有 retained bytes 或可恢复 source，可以在 Request freeze 前为该供应关系确定性建立新的 Binding generation；无法恢复原始材料时进入 reupload_required，而不是伪造材料可用。

一旦 Request 已冻结 **material_binding_snapshot**，恢复必须继续使用原快照，不因渠道优先级、Binding 状态变化或配置更新静默切换到另一渠道表示。

在多轮连续会话中，运行时材料绑定器（MaterialBinder）严格执行“本轮增量绑定（**bind_for_current_turn**）”与“历史引用核验及锁定（**verify_and_pin_historical**）”的双轨职责分离。

本轮实际需要投影的有效材料（**current_effective_material_ids**）参与当轮 Binding 准备与传输通道体积阈值（如 99 MiB）汇总评估。对于本轮模型可见的历史材料，其原生引用随历史消息进入上游请求，不再重复生成当轮附件 Part，但必须完成完整的 Provider Binding 生命周期核验。

**当本轮 Request 选择不继承历史时，历史原生附件引用不进入本轮 Provider Wire，但完整历史材料发生账本仍然必须经过核验。** Relay 继续根据执行面与数据库共用的 Provider-Neutral 绑定生命周期状态合同，验证历史材料的 Canonical Material 内容哈希、连接与账号作用域、绑定代次（generation）、目标 URI、绑定生命周期状态（**state**）与处理状态（**processing_state**）均已就绪，并确认剩余安全有效期满足要求；对有效历史资源建立必要的保护性 pin。

因此，历史材料即使不再对本轮模型可见，也不能被视为已经失去执行约束。若历史文件引用已经过期、被撤销或代次不匹配，系统仍必须在 **not_sent** 阶段（跨过不可逆派发门槛前）Fail-Closed 阻断并输出结构化诊断事件。

对于 **session_materials_only**，Session 锚定材料重新作为本轮有效材料参与 Binding；不得使用本轮不可见的历史材料发生记录省略其投影。

**模型上下文可见范围与材料资源安全校验范围相互独立。** 绝不允许为了维持调用而静默篡改已提交历史中的原生 URI，也不允许将失效历史材料偷偷塞回当轮附件。材料身份稳定，渠道切换只能重建执行表示，不能重写业务材料事实。

Material Binding 与 Cache Binding 是相互独立的执行事实：前者决定材料传输表示与跨轮次物理位置，后者决定上下文复用方式；详细边界见第 11.5 节。

| **概念**           | **稳定性**                               | **示例**                                                                       |
|--------------------|------------------------------------------|--------------------------------------------------------------------------------|
| material_id        | 业务/调用方稳定身份                      | mat_xxx；与 filename/source_ref/provenance 绑定                                |
| fallback object    | 可选耐久/桥接副本                        | Supabase private object；storage_id=supabase_shared                            |
| Provider Binding   | 可失效、按 connection/account scope 隔离 | gemini_file_uri / gemini_external_url / kimi_extracted_text / kimi\_\*\_ms_uri |
| binding generation | Request freeze 的副作用身份              | 同一 material 重新绑定时 generation 递增；已冻结 Request 继续复用原 snapshot   |

## 5.2 Gemini：Native Route、Material Transport 与 Provider Projection 三层分离

- Gemini inference 始终由绑定的 Gemini Native / GenAI SDK 连接实例承担；Material transport 不改变 inference route。**Relay 在 Request Binding 冻结前，依据当前冻结供应的 CapabilityContract（supported_material_transports 与 external_url_max_total_bytes）及完整材料集合的实际 actual_size 汇总值确定性选择传输通道**：

- **支持外部公网拉取的聚合渠道（如 AIHubMix Supply）**：合同配置 supported_material_transports = \[external_signed_url, provider_files_api\] 且切换阈值为 99 MiB；当材料总量 ≤99 MiB 时使用 Supabase External Signed URL，总量 \>99 MiB 时提升至 Gemini Files API。

- **不支持任意第三方外部 HTTPS Signed URL 直拉的官方直连渠道（如 Google 官方 Gemini Supply）**：由于官方原生 fileData.fileUri 要求传入官方 Files API 内部资源 URI（\[generativelanguage.googleapis.com/.../files/\](https://generativelanguage.googleapis.com/.../files/)\*），合同配置为 supported_material_transports = \[provider_files_api\]（禁用 external_signed_url 或阈值设为 0）；运行时 material_binder.py 无论材料体积是否超过 99 MiB，均确定性地通过官方连接装配的通用文件适配器（gemini_files.py）上传并绑定官方 fileUri。调用方提供的大小提示只用于诊断，不参与 transport 决策。

**Transport Selection 与 Provider Projection 是两个正交决策。** Transport Selection 回答“材料通过 External URL 还是 Files API 到达 Provider”；Provider Projection 回答“Canonical Material 应以何种 Provider-compatible MIME / 文件表示进入该 transport”。因此，Canonical Material 不因 Gemini 的输入限制而改变真实 MIME、文件名或业务身份。

对 External URL 路径，Relay 先从 Canonical Material 生成 Provider-facing bridge object，再为该对象签发短期 Signed URL。若源材料已经具有 Gemini 可直接消费的表示，例如 PDF，则 bridge object 保持原始 application/pdf 与文件表示；若文本型 JSON Artifact 的 Canonical MIME 为 application/json，则其业务材料仍保持 JSON 事实，但 Gemini-facing bridge object 投影为 text/plain，并使用与该表示一致的文本文件名。最终必须满足：**Gemini fileData.mimeType、Signed URL 实际 HTTP Content-Type 与 Provider-facing 对象表示保持一致。**

对 Files API 路径，Relay 仍以相同的 Provider MIME projection 规则准备上传内容，但不创建 External URL bridge；Provider File URI 只作为该 Material 在特定 connection / account scope 下的 Binding。两种 transport 最终均由 Gemini Native Adapter 组装为 fileData/fileUri，但都不得改变 Canonical Material 的身份和来源语义。

**材料传输能力与显式缓存资源创建能力必须按具体 ModelSupply 分别独立认证**，并通过 CapabilityContract.cache_contract 中的 **explicit_cache_material_mode（inline_bytes_only vs file_uri_allowed）** 合同化表达，绝不能将两者混为同一项供应能力或跨渠道互相推导：

- **inline_bytes_only 模式（如 AIHubMix 聚合供应）**：聚合链路实测不支持将 Files API 上传所得的文件引用用于创建显式缓存；因此 Files API 生成的 fileUri 仅作为普通推理输入的 Material Binding，创建显式缓存时必须使用 Relay 持有的内联材料字节，并严格受 **60 MiB 内联体积安全预算**约束。

- **file_uri_allowed 模式（如 Google 官方直连供应）**：官方原生 cachedContents 接口已认证支持直接引用官方 Files API 生成的 fileUri；当材料在当前官方连接与 account_scope_id 下已绑定有效的 gemini_file_uri 且满足上下文语义等价前提时，gemini_cache_projection.py 与 gemini_stateful_cache.py 直接将 fileData(fileUri, mimeType) 投影至显式缓存创建请求中，无需重复内联原始文件字节。

补充约束：缓存预算与材料传输阈值独立，最终请求总体积仍须单独校验，具体供应可以施加更严格限制。

Request freeze 前，BindingResolver 必须以 Request 实际材料总量重新确认 transport，并验证待使用的 Provider representation 是否符合当前投影规则。可恢复的旧 External URL bridge 若与预期 MIME / 文件表示不一致，应从 retained bytes 确定性重建 Provider Projection 并形成新的 binding generation；无法取得原始字节时进入 reupload_required，不得伪造可用 Binding。**一旦 Request 已冻结 binding snapshot，Resume 必须继续复用该快照，不得在恢复过程中静默切换 transport、projection 或 binding generation。**

material_id 始终保持稳定；Signed URL、bridge object、Gemini file URI 及 binding generation 都属于可过期、可重建、可审计的 Provider 执行资源。**Signed URL 是传输能力，不是材料事实；Provider Projection 是适配能力，不是业务数据改写。**

## 5.3 Kimi 与 Grok 的材料语义

Kimi Official File Adapter 按 MIME 选择 purpose：普通文本/文档使用 file-extract，并把官方抽取内容保存为 Provider-derived Relay artifact 后以 \[SOURCE_FILE:filename\] 边界注入；图片使用 purpose=image、视频使用 purpose=video，并以 ms://\<file_id\> 交给 MoonshotChatAdapter。Grok/ResponsesV2Adapter 没有 native file store 时，从 Relay material/fallback 解析文本、图片或签名 URL 映射到 input_text/input_image/input_file。

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<thead>
<tr class="header">
<th><p>实现备注｜<strong>未完全闭环机制提醒（完成度 &lt; 80%）</strong>：① <strong>Provider File Resumable Upload 跨进程崩溃续传恢复尚未完全闭环</strong>：虽然 gemini_files.py 已在二阶段分片流全面接入 TransportGuard 围栏校验且 material_binding_attempts 记录了尝试审计，但拿到临时 upload_url 后的持久状态机断点续传与自动 Reconcile 尚未完全闭环，进程在 start/finalize 间崩溃仍可能导致 Material 停留在 binding 状态。</p>
<p>原实现备注补充：Gemini prepare() 在拿到 resumable upload_url 后不会立即持久化该中间状态；若进程在 start/finalize 之间崩溃，幂等重试可能只返回旧 Material，无法按设计继续同一 upload_url 或自动 reconcile。</p></th>
</tr>
</thead>
<tbody>
</tbody>
</table>

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<thead>
<tr class="header">
<th><p>实现备注｜② <strong>端到端零内存聚合流式材料处理尚未闭环（完成度 &lt; 80%）</strong>：ObjectStorage 与 MaterialIngress 虽预留流式接口，但当前仍存在将分片聚合为内存 bytes 的环节。</p>
<p>原实现备注补充：SupabaseObjectStorage.put_stream 仍把全部 chunk 聚合为 bytes；API multipart 与 readable URL 也会把完整材料读入内存再交给 MaterialIngress。</p></th>
</tr>
</thead>
<tbody>
</tbody>
</table>

| **源码依据：**sql/004_provider_native_file_ingress.sql；app/materials/ingress.py；app/materials/binding_resolver.py；app/materials/gemini_transport.py；app/materials/provider_files/\*；app/materials/fallback_storage.py；app/persistence/object_storage.py |
|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# 6. Checkpoint 主恢复 + 幂等兜底 + Fencing：统一恢复机制

原文“Checkpoint 与 Idempotency 是同一条恢复协议的两个层次”继续成立，但当前正常恢复锚点已经从 job_id 提升为 request_id。Request 在 sync/async 两种模式下都存在；job_id 只在 async 时辅助排队、Worker 与运维诊断。

Checkpoint 主恢复 = 调用方优先依据 stage、session_id、request_id、可选 job_id、上下文引用等状态，从既有 Request 继续；一旦 request_id 已持久化，恢复只查询原 Request 的 status/result/error，不重新 execute，也不重新计算 options、capability 或 Provider Wire。Idempotency 幂等兜底 = 仅在 Relay 可能已受理但 request_id 尚未可靠写回的 PREPARED 窄窗口，以同一 Session、同一业务调用意图与稳定 Idempotency-Key 重放；input、instructions、materials、options、structured_output、execution 等所有会改变有效执行身份的字段必须保持一致。think_level 的幂等身份以 capability 解析后的 effective 语义为准：**对于固定 Thinking 的模型，不同调用方深度标签只要最终映射到同一个 Thinking ON，就仍属于同一执行身份，不应创建第二个逻辑 Request。** 若重放导致 effective options、effective think level、模型能力身份或其他执行事实发生变化，则 request_hash 冲突并拒绝复用。**恢复的是已经形成的原 Request，而不是重新执行，也不是重新解释一次调用方 UI 参数。**

| **统一原则：**先用 request_id 精确恢复；只有受理身份尚未形成可用 request_id 时，才用原 Idempotency-Key 安全重放。async 的 job_id 是调度身份，不应重新成为业务层唯一恢复主键。两者都不可验证时 Fail-Closed。 |
|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

<img src="model-relay-media/media/image4.png" style="width:6.6in;height:3.84759in" />

图 4 Request 级 Checkpoint 主恢复 + 幂等兜底：job_id 退回 async 调度身份

## 6.1 为什么仍必须成对设计

仅有 Checkpoint 时，系统能在 request_id 已保存后快速恢复，但仍覆盖不了“Relay 已接受 Request、调用方却在 request_id 回写前中断”的缝隙；仅有 Idempotency 时，重复提交可以命中原 Request，但调用方不知道业务链推进到哪个 stage。二者组合后，Checkpoint 提供业务恢复精度，Idempotency 封闭受理回写缝隙。

| **故障窗口 / 状态**                             | **Checkpoint 已知信息**                            | **Relay 事实**                   | **首选恢复动作**                                       |
|-------------------------------------------------|----------------------------------------------------|----------------------------------|--------------------------------------------------------|
| Request 受理之前                                | PREPARED：stage + session_id + K400；无 request_id | 无 Relay Request                 | 提交一次相同 Session Request                           |
| Relay 已受理，但 request_id 尚未落盘            | 仍是 PREPARED；只有 K400                           | R400 可能已经存在                | 以相同 K400 + 相同请求重放；命中原 R400                |
| R400 已落盘，sync/async 执行中                  | PENDING：S400 + R400 + K400；async 可含 J400       | Request running/queued；Job 可选 | GET Request；不要重新创建模型调用                      |
| provider_dispatch_state=not_sent 且 Worker 失租 | R400                                               | Provider 尚未发送                | Job 可安全被新 Worker 领取                             |
| provider_dispatch_state=dispatch_started 且失租 | R400                                               | 上游是否完成未知                 | 转 indeterminate；不自动重发 Provider                  |
| provider_dispatch_state=result_stored           | R400                                               | 完整 Provider 结果已落 Storage   | 重领后只 complete_request / history commit，不再次推理 |
| Request succeeded，下游业务未落盘               | RESULT_READY：R400                                 | compact/raw result 已存在        | 重复读取 result，从 Parser/Gate/Artifact 继续          |

## **6.2 Relay 内部恢复：跨层状态推进、统一 ExecutionFence 与心跳传输围栏**

**relay_requests.provider_dispatch_state** 依次为 **not_sent → dispatch_started → result_stored → committed**，其状态跃迁严格对应**运行时**与**执行面**的职责交接：

1.  运行时负责 not_sent → dispatch_started → result_stored：RelayRuntimeEngine 在完成本轮材料绑定、历史材料核验与缓存物化后，通过发送前原子封存（AtomicDispatchSealer）在单事务内建立本轮及历史资源 pin、封存绑定与材料位置摘要并将状态推进为 dispatch_started；随后由受控调用器（ControlledInvoker）调用一次协议适配层，在原始成功响应（raw-response.json）、提取输出（response-output.json）以及 **同时包含上游通用执行证据、思考观测事实（observed_thinking）与本轮物理投影材料发生事实（projected_material_occurrences 及计划摘要）的受控执行事实（execution-fact.json）** 全部落盘后，将状态推进为 result_stored 并向执行面返回 ActualExecutionFact。

2.  **执行面负责 result_stored → committed 及只提交恢复门禁**：ResultCommitter 接收 ActualExecutionFact 后，依次执行**归档事实完整性门禁校验、上游通用执行授权核验、思考执行合同后置核验与 Canonical Schema 后置校验**，全部通过后在同一数据库事务里更新 Session 历史版本、释放资源 pin 并将 Request 与关联 Job 推进至 succeeded（committed）。

为彻底消除失租进程（僵尸 Worker 或超时同步任务）的越权副作用，系统实施**数据库最终执行权校验、应用层心跳联动取消、底层传输围栏（TransportGuard）**三重防护：

- **数据库统一最终 ExecutionFence**：材料绑定安装、缓存绑定安装、发送前原子封存、结果落盘（result_stored）、成功提交（committed）及失败写入（failed）等所有状态变更 RPC，均强制核验 Request 当前派发阶段、非空 owner、正数 epoch、执行主体活跃状态以及严格晚于数据库 **clock_timestamp()** 的有效租约（异步同时核对关联 Job 租约，同步核对 Request 执行租约或只提交围栏）；**已失效或为空的租约绝不能通过续租复活**。

- **心跳失效主动终止、工厂级 TransportGuard 强制注入与全路径传输围栏**：执行面调度器同时维护执行任务与心跳续租任务；一旦续租返回 false、抛错或超时，立即永久标记本地执行权失效并取消执行子任务。为确保底层传输围栏无死角，**严禁任何推理适配器、文件上传适配器（如 gemini_files.py）、显式缓存管理器（如 gemini_stateful_cache.py）或第三方 SDK 封装层在内部私自创建裸 httpx.AsyncClient**。所有上游通信客户端均由公共协议与资源工厂（protocol_adapters/factory.py）按 connection_id 统一构建，并**强制注入 TransportGuard 拦截钩子与 http_wire_logger 报文诊断器**。运行时不仅在各物理阶段边界检查执行权，还在最底层 HTTP 传输实际发包前（**涵盖模型推理请求、SDK 文件上传初始化与 Resumable Upload 第二阶段向动态 upload_url 发送的每一个分片流、以及显式缓存创建/查询/删除网络调用**）通过 TransportGuard 再次核验活跃 ExecutionFence 及冻结连接指纹——即使协议适配层或文件/缓存适配器已完成报文组装，只要发包前失租，实际发往上游的 HTTP 调用次数仍严格为零。同时，http_wire_logger 随连接配置的 auth_type 自动扩展脱敏边界，对 Authorization: Bearer、x-goog-api-key 等多渠道鉴权头以及动态上传/下载 URL Query 中的敏感参数（如 ?key=、api_key、token）执行强制清洗脱敏。

- **not_sent 失租恢复**：当前执行者立即中止，仅在重新取得新 epoch 后才可安全重领执行；

- **dispatch_started 失租或流中断恢复**：包括已收到上游 2xx 响应头但响应体读取中断的窗口，权威恢复统一将其置为 **indeterminate**，保留 Session 占用与缓存资源 pin，**严禁换渠道或重发推理**；

<!-- -->

- 执行面负责 result_stored → committed 及只提交恢复门禁：ResultCommitter 在正常提交与旁路运行时的 result_stored 只提交恢复路径中，统一消费并强制核验 ActualExecutionFact（恢复路径从 execution-fact.json 重建并校验 placement_plan_digest 与发生列表完整性），依次完成归档事实门禁校验、上游通用执行授权核验、思考执行合同后置核验与 Canonical Schema 后置校验；全部通过后，结合本轮提交的原生 transport_history 哈希与精确 Part 摘要生成不可变的 **已提交材料发生凭证（CommittedMaterialOccurrence）**，在同一数据库事务里核对封存记录、推进 Session 历史版本、**将新增材料发生凭证原子追加至会话已提交材料账本（committed_material_ledger）**、释放活跃资源 pin 并将 Request 与关联 Job 推进至 succeeded（committed）。任何归档材料位置事实缺失或与封存事实不符的恢复请求均直接阻断，绝不带病提交或触发二次推理。

缓存资源副作用采用独立的 Operation / Resource 双重围栏，并在首次模型发送封存门槛与 Request 执行权汇合；相关恢复规则见第 11.7 节。

## 6.3 调用方 Checkpoint 的责任仍在 Relay 之外

当前 Relay Server 没有业务 checkpoint 表。调用方建议保存 checkpoint_version、pipeline/stage、session_id、request_id、async job_id、idempotency_key、request_hash、owner、provider/model/生效参数、schema identity、execution site，以及后续继续所需的小型 Artifact 引用。业务 stage 可以作为 opaque metadata 进入 Relay 日志/存档，但 Relay Core 不按它分支执行。

## 6.4 可靠性边界：逻辑 Request 幂等仍不等于 Provider 严格 exactly-once

当前实现采用更保守的恢复语义：失租不会盲目重放已经 dispatch_started 的模型调用，sync timeout 也不会自动改建 Job；但 Provider 请求本身仍未统一携带一个 Relay 可控制、所有厂商都支持的 exactly-once 幂等协议。因此“同一个 Relay Request 不被调用方重复创建”与“上游绝不可能产生第二次计费/副作用”仍是不同保证。

跨渠道故障切换属于“新的执行事实形成之前”的路由选择能力，不属于 Request 恢复机制。尚未创建并冻结 Session / Request 时，可以在合格供应集合内按路由策略选择下一候选；一旦 Session / Request 已冻结具体 Model Supply，恢复路径必须围绕原执行事实进行，不得把渠道切换伪装成原 Request 的普通重试。

对于已冻结但 provider_dispatch_state=not_sent 的 async Request，Lease 过期仍只允许在原冻结供应关系上安全重领；若业务确实要求改走另一渠道，应显式形成新的 Session / Request 执行身份并保留与原 Request 的迁移关系。dispatch_started 后发生失租、进程消失或上游结果未知时继续进入 indeterminate，不自动换渠道重发；result_stored 只恢复 complete / history commit，不重新调用任何渠道。

**渠道优先级不能穿透 Request recovery；优先级决定新的合格供应如何被选择，不授权对已经形成的外部执行事实静默改道。**

| **可靠性备注：**indeterminate 表示“Relay 知道执行身份，但无法证明 Provider 最终是否完成”。当前实现没有通用 provider response_id 对账服务来自动把所有 indeterminate 恢复成 succeeded/failed；调用方可以查询、人工/厂商对账或取消 Session 占用，但不能把未知执行伪装成未执行。 |
|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

| **源码依据**：**app/execution_plane/result_committer.py**；**app/execution_plane/schedulers/**\*；**app/runtime/engine.py**；**app/runtime/execution_guard.py**；**app/runtime/transport_guard.py**；app/v2_repository.py；sql/003\_\*.sql；sql/010_execution_plane_commit_recovery.sql；sql/011_execution_authority_closure.sql |
|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# 7. 工作流示例：答案融合模式如何应用当前恢复机制

原文以 Final Draft Generation 为例说明“昂贵阶段不应因 Dify Workflow 中断而从头重跑”。这个业务原则不变；变化是 Relay 侧的主身份从 J400 提升为 Request R400。假设 Final Evidence Review 与 Blueprint 已完成，应用层准备执行耗时较长的 Final Draft。

## 7.1 正常执行：模型调用前后各写一次轻量状态

| **时点**                         | **答案融合状态**             | **Relay 身份**                                          | **作用**                                             |
|----------------------------------|------------------------------|---------------------------------------------------------|------------------------------------------------------|
| 进入 Final Draft 前              | PREPARED / stage=FINAL_DRAFT | session_id=S400 + idempotency_key=K400；request_id 暂空 | 即使提交已经到 Relay、响应未写回，也有安全重放依据   |
| Relay 接受 Request 后            | PENDING / stage=FINAL_DRAFT  | request_id=R400；若 async 则 job_id=J400 + K400         | R400 成为正常恢复锚点；J400 只承担队列调度           |
| Provider 成功并提交 Request      | RESULT_READY                 | R400 → compact result + raw/output objects              | 从 Parser/Renderer/Quality 继续；Checkpoint 暂不清除 |
| 业务 Artifact / state_patch 落盘 | DONE                         | R400 作为审计引用                                       | Checkpoint 才推进/清理                               |

## 7.2 中断后的主路径：直接恢复原 Request

假设 Final Draft 使用 async，当前 Workflow 在短等待窗口内尚未完成而结束。下一次 /fusion finalize 或恢复入口读取 Checkpoint 后，如果已有 R400，就查询 /v2/sessions/S400/requests/R400；queued/running 继续等待，succeeded 读取 result，failed/indeterminate 读取 error 并进入对应业务处理。恢复动作不需要再次 POST 一个新的模型 Request。

拿到 compact result 后，结果重新接回应用层 Synthesis Artifact / Body 聚合、Final Plan Parser、Renderer、Quality Gate 等节点。Direct Final Synthesis、Global Adjudication 等业务阶段如何组织仍属于应用层，不由 Relay Core 判断。

## 7.3 异常缝隙：request_id 没写下来时由幂等兜底

最棘手的窗口仍是：Dify 已把同一业务请求 K400 送到 Relay，Relay 可能已经受理为 R400（async 时也创建 J400），但调用方在保存 R400 前中断。此时 Checkpoint 仍停留在 PREPARED；恢复逻辑使用完全相同的 Session、输入、材料、schema、execution mode 与 K400 再次提交。Request 幂等先比较 request_hash：相同则返回原 R400，不同则 409，避免同键绑定不同调用。

## 7.4 为什么 Checkpoint 不能在模型一成功就清掉

如果 R400 已 succeeded，但应用层在 Parser、Renderer、Gate 或 Artifact Assigner 中失败，昂贵模型结果已经存在。下一轮应再次读取 R400 result 并从下游继续；只有最终业务 Artifact / state_patch 确认持久化后才清理或推进 Checkpoint。这个原则与旧文档完全一致，只是恢复锚点改成了 Request。

| **集成备注：**本源码包不包含当前 Dify Tool/Plugin 源码，因此“Dify 如何保存/读取 Checkpoint、是否已从 job_id-only 工具迁移到 request_id”不能从 Relay 包直接验证。本文在本节保留的是 Relay 能力与推荐集成协议，不把调用方实现描述成已落地事实。 |
|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# 8. 订单支付类比：同一套稳定身份 + Checkpoint + 幂等机制

成熟电商不会把“订单/支付状态恢复”和“支付幂等”当成两套孤立功能：正常恢复依赖稳定 payment_id 与订单状态机，受理响应丢失时依赖 merchant_payment_no 之类的业务幂等号。当前架构中，Request R400 比旧版 Job J400 更像 payment_id；Job 更像支付平台内部的后台调度任务。

<img src="model-relay-media/media/image5.png" style="width:6.6in;height:3.90219in" />

图 5 答案融合与订单支付：Request / payment_id 是执行事实，幂等键封闭受理回写缝隙

## 8.1 从支付意图到最终完成的逐步映射

| **故障窗口 / 语义** | **答案融合模式**                            | **订单支付场景**          | **双层机制如何协同**                               |
|---------------------|---------------------------------------------|---------------------------|----------------------------------------------------|
| 稳定业务意图        | Final Draft request → K400                  | merchant_payment_no=M1001 | 幂等键定义“这是同一次业务动作”，不是恢复位置       |
| 外部执行事实已创建  | request_id=R400；async job_id=J400          | payment_id=P1001          | Checkpoint 保存“已经走到这里 + 外部执行事实是谁”   |
| 应用/Workflow 中断  | 下一次 finalize 读取 R400                   | 应用重启后查询 P1001      | 主恢复直接查原执行事实，不从业务起点重放           |
| 受理响应丢失        | 只有 K400，未知 R400                        | 只有 M1001，未知 P1001    | 使用同一稳定幂等键重放受理，返回原执行事实         |
| 后台调度重试        | J400 可重领，但 dispatch_started 不盲目重发 | 支付平台内部 worker 重试  | 调度身份与业务执行身份分离；fencing 防旧执行者提交 |
| 业务提交完成        | Artifact/state_patch DONE                   | 订单 PAID / 履约          | 只有业务状态落盘后才清理 Checkpoint                |

## 8.2 这个类比说明了什么

- 成熟系统的正常恢复靠状态机 / Checkpoint + 稳定执行身份：支付恢复查 P1001，答案融合恢复查 R400，而不是重放整条业务链。

- Idempotency 是安全受理边界：只有 request_id / payment_id 尚未可靠落盘时，才用相同业务键重复提交；已经有稳定执行身份后优先查询。

- Job 是后台调度身份，不应该再被等同于业务执行身份；这是当前架构相较旧基线的重要语义收敛。

- Checkpoint 不能替代幂等，幂等也不能替代 Checkpoint；一个解决“从哪里继续”，一个解决“受理结果丢失时重放是否仍是同一次动作”。

| **一句话概括：**订单系统用 PAYMENT_PENDING + payment_id 做主恢复，用 merchant_payment_no 防重复受理；答案融合用业务 stage + request_id 做主恢复，用 Idempotency-Key 防止 PREPARED 回写缝隙创建第二个 Request。async job_id 只承担后台调度与运维诊断。 |
|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# 9. 通用穿透、路由与业务语义边界

<img src="model-relay-media/media/image6.png" style="width:6.6in;height:2.98424in" />

图 6 v2 通用控制面：业务语义留在调用方，RouteBinding/Binding/Provider 传输留在 Relay

## 9.1 v2 公共控制面与 Dify 显式 V2 传输门面

标准主接口是 /v2/materials、/v2/sessions、/v2/sessions/{session_id}/requests 及其 status/result/error/cancel。对于 /v1/dify/relay 网关入口，系统实施**显式契约分流治理**：未指定 execution_contract 时保持旧版 /v1/jobs 兼容路径；当调用方显式声明 **execution_contract="v2"** 时，由轻量级传输门面（dify_v2.py）通过服务端私有的 **TrustedDifyEntry ASGI 作用域包装器**，将 execute、status、result 操作在进程内直接桥接至原生 v2 HTTP 路由（完整复用 v2 鉴权、SessionManager 与 RequestAcceptor）。显式传输门面严守三项架构红线：① **严格 Schema 校验与未知字段拒绝**，严禁将旧版业务阶段字段静默丢弃后放行；② **以 exclude_unset=True 无损透传字段显式存在性**，确保调用方“省略 think_level”、“传空值”与“显式传 AUTO”的语义边界在跨门面转发时零失真；③ **服务端可信入口审计**，仅采信 ASGI 内部作用域标记的真实入口身份，忽略任何外部客户端伪造的同名 Header，且审计入口不参与 request_hash 计算，不改变重放与恢复事实。

## 9.2 Request Envelope 的通用性

**SessionRequestCreate** 的 canonical 字段包括 **input**、**instructions**、**material_ids**、**context_policy**、**think_level**、**think_level_policy**、**options**、**requested_cache_mode**、**execution**、**structured_output**、**metadata**。其中 **context_policy** 为可选的 Request 级上下文策略字段，支持 **conversation**、**explicit**、**session_materials_only** 三种取值；省略时继承 Session 创建时冻结的默认策略。

**Request 级 \`context_policy\` 与 Session 创建时的 \`context_policy\` 属于不同作用范围。** 前者只决定本轮模型可见上下文，不改变 Session 的默认策略及历史累计机制；**session_materials_only** 仅作为 Request 级策略使用，不改变 Session 创建合同。

其中 **think_level** 统一采用 AUTO、OFF、LOW、MEDIUM、HIGH、XHIGH、MAX 七态标准请求意图（缺省统一为 AUTO），**think_level_policy** 支持 **prefer**（默认普通偏好适配）与 **require**（硬性能力要求）两种约束策略，不向业务层暴露底层 Token 预算数值、推理开关结构或厂商专属字段。

requested_cache_mode 是每轮 Request 的公共意图字段，允许 off / auto / on，不暴露 Provider 原生缓存参数；默认、幂等和强制缓存的具体语义见第 11.4 节。

公共请求体不开放缓存资源有效期、读写策略、共享范围、资源句柄、断点位置或提供方原生缓存字段。具体机制、适用性和准备失败行为由统一缓存控制层决定，详见第 11 章。

options 继续承载 temperature、top_p、max_output_tokens 等 Provider-Neutral 高级模型控制，由 Relay 结合**本轮解析出的有效思考语义执行跨维度联合校验**后形成 effective_options（当思考处于激活态且合同声明禁用自定义温度、禁用 top_p、要求固定温度值或要求思考预算严格小于最大输出上限时，在受理阶段直接完成互斥拦截，且**绝不主动补写被思考合同禁止的默认采样值**）。业务调用方不得发送 generationConfig、max_tokens、thinkingConfig、reasoning_effort 或其他 Provider Native JSON 来决定 Wire 形状。请求入口只允许可无歧义映射到 canonical intent 的已知受控别名（如 none、disabled、false 受控归一化为 OFF）；未知或试图覆盖 Relay 内部思考与缓存控制的 Provider 字段直接拒绝，同一语义出现冲突值时 Fail-Closed。provider/model/connection hint 只能与 frozen Session 做 assertion，不能改写 route。调用方表达 requested intent，Relay 冻结 effective semantics；思考与缓存的物理机制和 Native Wire 字段始终属于 Relay 内部执行事实。

## 9.3 服务端权威路由：provider/model 是业务意图，connection 是内部事实

服务端权威路由应从“provider/model → 唯一 connection”扩展为“model intent → 合格 Model Supply 集合 → 按模型配置的渠道优先级选择 → 冻结 RouteBinding”。Model Supply 表示一个模型通过某个渠道、账号作用域和 Protocol Profile 被提供的具体服务关系；同一模型可以存在多个 Model Supply，并分别配置适用范围和 priority。priority 不是全局渠道排名，而是该模型、该策略作用域下合格候选之间的选择顺序。

路由按对外模型与部署环境识别受管供应组，遵循**先形成资格事实，再应用路由限制，最后按渠道顺位选择**的顺序。对全部启用供应分别评估租户、区域、凭据、安全、材料可达性、模态、思考与结构化保证、协议兼容及必要的资源条件。渠道优先级只决定合格供应之间的选择，不能通过删除参数、丢弃材料或降低能力要求，使不合格供应重新获得资格。

渠道的“不可用时”策略表达新会话的向下切换限制。某行不合格且允许尝试下一顺位时，可以继续选择较低顺位的合格供应；某行不合格且要求拒绝新会话时，该行以下的供应被阻断，其上仍存在合格供应时可以正常选择。停用行不参与候选，也不触发该行的拒绝规则；适用于整个供应组的安全禁止直接终止选择。调用方限定渠道只能收窄授权范围，不能消除平台已经配置的拒绝边界。

资格未知应按原因处理。安全授权、必要能力或收费预算无法确认时，不得判定为合格；健康信息缺失是否允许继续，由已发布策略明确规定。动态健康、配额和预算只有具备权威来源、有效时点及必要的并发控制时，才能参与自动选择。资格评估不通过试发业务推理、上传文件或创建缓存资源探测供应。

需要预算或并发预留时，应在候选选定后、外部副作用发生前完成，并与受理身份关联。会话建立前的预留失败，只有在确认没有外部副作用时，才可按既定策略重新决策。**会话建立后的额度、健康或顺位变化，只影响后续执行准入，不授权重新选择渠道。**

受管供应组独立于可用候选存在。即使全组暂停或没有合格供应，也必须保留受管范围，阻止请求重新落入旧通配路由。停用供应不得进入可选目录；旧配置入口与受管配置存在覆盖冲突时，应阻断激活或先完成迁移，不能依靠配置来源优先级绕过治理规则。

Session 创建时冻结供应、账号作用域、协议、能力合同、路由策略，以及经批准的渠道侧模型映射。**即使首次模型请求尚未发送，也不能因渠道顺位或健康变化静默迁移会话。**需要改走其他渠道时，应显式形成新的会话与请求身份并保留关联。

- 上游渠道内部的模型替换、协议降级、收费兜底与原生受限生成履行情况，统一受控制面发布的执行授权合同（execution_authorization）约束，调用方参数不得覆盖。合同明确冻结是否允许渠道内部 fallback（allow_provider_fallback）、是否允许免费转付费兜底（allow_paid_fallback）及必填预算引用（paid_fallback_budget_ref）、允许的实际模型集合（allowed_actual_models）、预期计费层级（expected_billing_tier）、计费未知时的处理策略（on_unknown_billing）、是否强制要求原生约束执行证据（require_native_constraint_evidence）以及证据采信模式（evidence_source_mode）。

为避免将多模型聚合网关的防御规则错误强加给第一方官方直连端点，系统将 ProtocolProfile 的网关/连接交互特征与底层适配器实现解耦，并在协议适配层（native_evidence.py 与 thinking_evidence.py）对通用执行授权与思考执行事实实施**双模证据提取**：

- **聚合网关响应头取证模式（gateway_headers）**：适用于 AIHubMix 等聚合供应，协议适配层严格按冻结在网关级 ProtocolProfile 中的已登记响应头提取实际模型、计费层级、付费兜底、协议降级、原生约束执行、JSON 修复，以及**思考参数剥离（thinking_parameter_stripped）、思考降级（thinking_degraded）与上游有效思考模式观测值**；仅采信协议配置中显式声明的响应头，缺失或未配置时统一记为 None（unknown），绝不臆测上游状态。

- **第一方直连与原生响应体取证模式（first_party_native_payload 及各协议负载解析）**：适用于 Google 官方 Gemini API 等第一方原生直连供应及各协议原生响应体，结合直连受控身份与原生 2xx 响应负载提取实际模型、直连计费层级、原生约束执行证据，并从各协议原生结构中提取**上游思考观测证据（ObservedThinkingEvidence：涵盖 Gemini thoughtsTokenCount 与 thought 分块、OpenAI/Responses reasoning_tokens 与 reasoning 输出项、Claude thinking/redacted_thinking 内容块、Moonshot/ChatCompletions reasoning_content）**。**未上报的思考统计严格保持 None（unknown），绝不按零消耗或已关闭处理，且思考 Token 数量不被当作已达到特定思考深度的履约证明**。

运行时在收到 2xx 响应后，先将原始响应、输出及包含 observed_thinking 的执行事实无损归档至 execution-fact.json 并推进至 result_stored；随后由执行面（ResultCommitter）在正常提交与 result_stored 恢复路径上，同时按冻结的 execution_authorization 合同与 thinking 合同执行后置核验：任何未经授权的模型替换、缺少预算引用的免费转付费兜底、协议降级、违反未知计费/原生约束证据要求，以及**上游剥离思考控制参数、在硬性要求（policy=require）或禁止降级策略下发生思考模式降级/偏离、或在有效思考状态为关闭（如明确请求 OFF）时上游响应仍出现显式思考内容块或正数思考 Token 等确定性违约行为**，均直接判定为执行合同失败并完整保留原始 2xx 响应引用供审计，绝不触发二次推理补偿。

## **9.4 Structured Output 的跨层解耦：适配层 Wire 投影与执行面 Canonical 后置校验**

Structured Output 在架构上严格拆分为**执行面合同治理**与**协议适配层 Wire 投影**两层职责，避免在单一模块中混杂协议转换与业务验收：

- **执行面受理与冻结**：调用方提交的 JSON Schema 始终作为不可变的 **Canonical Application Contract** 在**执行面**解析并随 Request 快照冻结。Capability Contract 明确声明 structured_output 的保证模式（**native_constrained** 或 **post_validated**），无法满足调用方硬约束的请求在受理阶段直接 **Fail-Closed**。

- **协议适配层确定性 Wire 投影（schema_projector）**：**协议适配层**只读消费已封存的 Canonical Schema，依据 ProtocolProfile 将其确定性投影为 Provider 原生格式（如削除或改写特定 Native Wire 不支持的关键词以生成 responseSchema 或 response_format），**但不修改 Canonical Schema 本身**。

- **执行面后置校验与原生约束证据核验（ResultCommitter）**：在**运行时**完成原始 2xx 响应归档（**result_stored**）后，**执行面**使用冻结的原始 Canonical Schema 对提取结果执行最终 Draft 2020-12 校验。当调用合同要求 **native_constrained** 时，执行面还会联合核验协议适配层提取的上游观测证据：**若上游明确报告移除了原生 Schema 约束、发生了协议降级、或经过了上游 JSON 修复，抑或在 require_native_constraint_evidence=true 时缺少原生约束执行证据，即使返回文本偶然符合 JSON Schema，执行面仍将其判定为合同失败并保留原始 2xx 响应存档，绝不将“JSON 碰巧合法”伪装成“上游已履行原生受限生成合同”。**

## 9.5 Fusion 业务边界：v2 Core 已解耦，legacy 仍保留

当前实现中，v2 Core/Execution/Provider 代码不再根据 global_adjudication、final_draft_generation 等 Fusion stage 决定执行行为；tests/test_v2_boundaries.py 也专门验证 v2 Core 没有 Fusion stage branching。

但是 app/fusion_runtime.py、sql/002_fusion_runtime.sql 与旧 Job/Fusion 合同仍在仓库中，app/worker.py 对 protocol_version=v1 / 无 request_id 的 Job 会懒加载 app.compatibility.legacy_worker。因此准确表述是：新 v2 主路径完成业务解耦，旧 Fusion Runtime 作为兼容路径继续存在，并未从仓库物理删除。

| purpose 属于 Route Policy 的可选资格维度，而不是业务 stage 分支。若某模型的不同用途共享相同供应合同，purpose 可以不改变候选集合；若治理策略为特定 purpose 配置了渠道、能力、安全或成本约束，则不满足该约束的 Model Supply 必须在优先级排序前被排除。purpose 只用于声明执行约束，不允许 Relay Core 据此解释 Candidate、Conflict、Final Draft 等业务语义。 |
|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

| **源码依据**：app/v2_models.py；app/api_v2/router.py；**app/control_plane/catalog.py**；**app/control_plane/definitions.py**；**app/control_plane/execution_authorization.py**；**app/execution_plane/route_resolver.py**；**app/execution_plane/supply_resolver.py**；**app/execution_plane/structured_output.py**；**app/execution_plane/result_committer.py**；**app/protocol_adapters/wire_projection/schema_projector.py**；app/compatibility/legacy_worker.py |
|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

## 9.6 配置治理控制面

控制面以**五维正交身份模型**协同策略合同实施统一治理：**Model Catalog** 维护稳定模型逻辑身份、厂商归属、生命周期与别名；**Channel / Account** 维护供应来源（如 google_official 与 aihubmix）、云账号/项目、区域与数据驻留边界；**Connection（ConnectionSpec）** 独立定义具体网络端点（明确区分 rest_base_url、upload_base_url 与 sdk_endpoint_options）、API 版本、鉴权类型（auth_type，如 bearer_header 或 x_goog_api_key_header）、受控凭据引用（credential_ref）及主机白名单；**Model Supply** 将具体模型与特定渠道账号、ConnectionSpec、CapabilityContract、ProtocolProfile 及 CachePolicy 绑定为可调度的供应单元；**Protocol Profile** 描述可复用的 Wire 语法、用量映射、条件化亲和头策略及响应证据映射。**密钥明文绝不进入普通配置、编译快照或请求日志**，Settings、控制面与运行时共享同一套凭据解析与授权作用域（account_scope_id）及连接指纹（connection_spec_hash）计算规则，旧环境变量通过兼容转换层（compatibility/connection_config.py）自动映射为标准 ConnectionSpec。

具体 Model Supply 的缓存有效能力与认证条件，以及 Cache Policy 的职责，统一见第 11.2 节。

配置生命周期采用“草稿 → 结构校验 → 合同校验 → 受控真实调用验证 → 审核 → 灰度 → 发布 → 回滚 / 退役”。模型自动发现、渠道目录存在或文档宣称支持，只能生成候选配置，不能直接获得生产流量。每项关键能力都应保留来源、验证证据、责任人和复核状态；凭据只以受控引用存在，不进入普通配置正文。

执行面只消费已经发布且通过一致性检查的 Config Snapshot。控制面临时不可用时，运行节点可以在策略允许的期限内继续使用最后一个已验证快照；不存在可靠快照、快照与执行节点能力不兼容或安全策略已撤销时，对新的不确定调用 Fail-Closed。紧急安全禁用可以阻止新的受理，但不得重写已经保存的 Request / result / error 事实。

资源治理与配置治理统一管理。Model Supply / Route Policy 应定义适用的并发、请求频率、token 配额、成本预算和共享账号作用域；多个 API / Worker 不能各自把同一账号的完整额度当作独占额度。免费供应转付费供应、跨区域数据传输、扩大模态或工具权限等行为必须通过显式策略授权。

**配置发布不是把任意 Provider JSON 或脚本塞进配置；配置只能组合 Relay 已支持并经过验证的公共能力。协议扩展属于平台升级；已有协议和运行时能力覆盖范围内的新模型接入才属于零代码配置发布。**

**结构化表格与管理页面是配置治理的声明入口，用户编辑的是目标配置，运行时执行的是经过校验、认证和发布的冻结合同。**模型清单表达公共服务目标，渠道优先级表达同一模型在指定环境中的供应顺位与切换限制，缓存设置表达具体供应的缓存策略申请。模型级目标不自动成为每个渠道的实际能力，启用意向也不直接构成生产授权。

用户界面只展示可理解的模型、渠道、策略和审核状态。供应身份、凭据引用、协议配置、认证证据及配置摘要由后台维护，不要求用户填写机器标识。渠道名称必须解析到受控登记的账号、区域和端点作用域；作用域不同的渠道分别登记，名称变化保留原有映射与审计关系。

导入器负责解析和规范化声明，合同校验器负责关联已登记能力、协议实现与认证证据，发布服务负责变更预览、审核和快照激活。**导入动作不访问执行中的会话，不创建材料或缓存资源，也不隐式触发收费推理。**真实渠道认证由独立验证任务在明确的账号、权限和预算下执行，其结果作为发布依据，不能通过表格单元格自行授予。

服务端独立校验字段、引用、作用域内唯一性和约束组合，不信任表内公式或本地检查结果。草稿、停用项和规划占位项可以保留用于管理，但不能获得流量。省略某行不表示删除既有配置；删除、暂停和受管范围变化必须进入明确的变更预览。发布时校验基准快照，存在并发修改时重新比较，不能覆盖他人已经生效的配置。

Excel 工作簿（workbook.py / thinking_mappings.py / auto_workbook.py）、声明式配置（declarations.py）与管理端复用同一治理模型及交叉校验规则。日常配置变更的操作约定为：用户修改 Excel、配置密钥并重新部署；系统在部署初始化阶段自动读取工作簿、校验配置、编译并保存不可变运行快照，再自动加载为运行配置。快照的编译、保存及发布属于系统内部步骤，用户无需另行生成或维护发布 JSON 或激活指针；请求热路径只读消费已加载的不可变快照，不直接解析或改写 Excel。

在思考能力配置治理上，系统确立以「思考映射清单」（协同「思考合同配置」与「供应思考绑定」）为单一可信配置源（Single Source of Truth）的架构：**直接删除《模型清单.思考档位》列，并彻底取消《能力合同》中的重复白名单（accepted_levels）与分散预算列**，消除多表重复维护与规则冲突；支持通过「思考合同配置」复用基础非思考能力合同并附加思考激活约束，通过「供应思考绑定」在不扩大原供应模型范围的前提下按模型模式（如区分固定开启模型与离散控制模型）绑定独立思考合同与渠道顺位优先级。离线编译器在编译期对思考映射实施严格的静态门禁校验，将运行时的参数错误前移至部署编译期阻断（精确定位到具体工作表名、行号、合同 ID 与违规字段）：

1.  **七态完备性与唯一性校验**：每个启用的思考合同必须完整声明 AUTO、OFF、LOW、MEDIUM、HIGH、XHIGH、MAX 全部 7 条映射规则，严禁缺失或重复；且 AUTO 档位必须允许调用。

2.  **六种模式与投影动作自洽校验**：严格核验有效思考模式（none / toggle / fixed_on / discrete / budget / adaptive）与原生投影动作（omit / send_level / send_budget / send_toggle / send_adaptive）、有效档位及整数预算上下限的合法组合。

3.  **跨档位单调性校验（Monotonicity Guard）**：在 LOW → MEDIUM → HIGH → XHIGH → MAX 五级强度上，离散模式的目标档位序级与预算模式的有效 Token 预算必须满足非严格单调递增，严禁高阶请求意图反而映射到更低档位或更小预算的倒挂配置。

4.  **OFF 与固定开启安全边界校验**：OFF 意图严禁映射到任何开启思考的执行状态（即使在 prefer 策略下也不允许将关闭思考静默映射为开启）；fixed_on（固定开启）模式必须拒绝 OFF 请求，且不得伪造宣称满足 LOW～MAX 的特定深度硬约束（satisfies_require 必须为 False）。

5.  **自动推导供应准入摘要**：编译器从每个合同的 7 条映射规则自动聚合生成供应级思考能力摘要（包括主导模式、允许意图集合与满足硬约束的 hard_satisfied_intents 集合），直接赋能会话准入选路。

6.  **拒绝规则完整性校验**：当 is_allowed=FALSE 时，必须同时满足 satisfies_require=FALSE，且 mapping_reason 必须提供明确、可审计的拒绝原因；禁止出现“不允许调用却声明满足硬约束”或拒绝原因缺失的自相矛盾配置。

针对思考、缓存与高级能力的发布，编译器全面实施“能力合同声明（CapabilityContract） ∩ 协议映射实现（ProtocolProfile） ∩ 已注册运行时/投影适配器能力”的三方交集准入与跨合同自洽性校验（例如：校验思考投影策略与供应协议族及动作集合完全匹配；启用 file_uri_allowed 显式缓存模式时，强制校验同一供应的 supported_material_transports 必须包含 provider_files_api 且文件适配器已就绪），并且新增纳管供应统一纳入自动部署验证门禁（未完成真实验证或缺失凭据时保持 pending/unavailable，绝不产生假就绪），最终输出深度只读的 ImmutableConfigSnapshot。

# **10. Provider-Neutral 与模型适配层**

Provider-Neutral 执行链严格映射在**四层单向契约**之上：**控制面**发布 **ImmutableConfigSnapshot** → **执行面**完成供应选路与有效语义解析，生成并冻结 **Request、ContextPlan、CacheIntentPlan**（封装为 **FrozenExecutionPlan**） → **运行时**落实 **Material Binding** 与 **CacheExecutionBinding**，完成发送前原子封存（**SealedDispatchBundle**）并触发单次受控调用，归档**实际执行事实（ActualExecutionFact）** → **协议适配层**只读消费封存输入，投影 **Native Wire** 并返回**标准化 Provider 结果（StandardizedProviderResult）** → **执行面**完成最终合同校验与事务提交。**模型、渠道、协议与缓存机制彼此独立治理：模型回答“调用谁”，渠道回答“从哪里获得服务”，协议回答“如何表达与解析调用”，缓存机制回答“上下文以哪一种受治理的物理方式复用”。**

**缓存策略、机制选择与物理绑定不得在协议适配层内重新决策**；完整上下文投影和发送封存约束见第 11.5～11.7 节。

当一个新模型能够被现有 ProtocolProfile、CapabilityContract 表达能力以及**执行面、运行时、协议适配层**完整覆盖时，接入动作只需要在**控制面**登记模型、配置 ModelSupply、声明能力合同、设置渠道优先级并编译发布新的配置快照，各层执行代码与业务 Workflow 均不增加模型专属分支。**已有协议与平台能力覆盖范围内新增模型实现零代码接入。**若新模型引入新的协议语义、不可复用的历史续接、未支持模态或无法由现有能力系统表达的执行行为，则先升级平台层能力，再通过配置开放模型。

## 10.1 Capability Contract：参数、思考与执行保证

CapabilityContract 不是“支持哪些字段”的静态白名单，而是一组驱动跨模型能力适配的可执行约束合同。它至少描述输入模态、上下文与输出资源限制、参数取值范围、默认行为、参数之间的依赖与互斥、Structured Output 保证级别、工具/流式能力、历史续接方式、材料表示要求以及**思考能力映射合同**。缓存作为 CapabilityContract 的独立子合同，由 ProtocolProfile 与 CachePolicy 协同约束（详见第 11.2～11.3 节）。

在思考能力治理上，系统彻底摒弃“先检查静态白名单、不匹配即抛 422 阻断”的参数校验模式，确立“七态标准请求意图、偏好/硬约束双轨策略、单一映射清单驱动、六种有效控制模式、三层语义闭环、注册式原生投影”的完整能力治理机制：

1.  **七态标准请求意图与 AUTO 默认准则及省略语义保护**：

    - 公共请求意图统一标准化为 **AUTO、OFF、LOW、MEDIUM、HIGH、XHIGH、MAX 七态**（支持将 none / disabled / false 作为 OFF 的受控别名；未知非法值直接拒绝）。

    - 由于不同模型的同级标签不代表相同的推理预算或计算强度，Relay 统一以 **AUTO 作为缺省请求意图**，且 AUTO 是独立语义，不参与 LOW～MAX 的档位距离计算。

    - **严格区分“省略参数（omitted）”、“显式空值（explicit_empty）”与“显式传递 AUTO（caller_explicit）”**：当调用方省略参数或传入空值且命中 AUTO 规则时，除固定开启（fixed_on）模型外，系统强制采用 wire_action="omit" 保持省略语义，绝不擅自向上游补写未请求的思考参数；当调用方显式传入 "auto" 时，则依据合同配置的显式 AUTO 省略策略（omit_on_explicit_auto）决定是继续省略参数，还是显式发送原生自适应配置（send_adaptive）。

2.  **普通偏好（prefer）与硬性要求（require）双轨策略及 satisfies_require 合同裁决**：

    - 调用方通过 think_level_policy 表达本次思考意图是**普通偏好（prefer，默认）还是硬性能力要求（require）**。

    - 执行面遵循“先查询冻结合同映射表，再校验允许状态（is_allowed）与硬约束满足度（satisfies_require）”的执行顺序。运行时绝不通过数值距离猜测“相近档位”，一切降级或钳制（如将超档偏好 XHIGH / MAX 钳制到模型最高支持的 HIGH，或将不支持的普通档位偏好收敛至 AUTO）均由 Excel「思考映射清单」显式定义。

    - **硬约束判定基于合同显式声明的 satisfies_require 布尔字段，而非字面字符串相等**：预算型（budget）模型将 HIGH 映射为整数 Token 预算，字面虽不同但实质原生满足硬约束（satisfies_require=True）；反之，降级适配（如 MEDIUM 降级为 LOW、仅支持自适应的模型将 LOW 收敛为 AUTO、或固定开启模型接收特定深度请求）其 satisfies_require=False，仅在 prefer 下放行，在 require 下严格执行 Fail-Closed（THINK_LEVEL_REQUIREMENT_UNSATISFIED）。

3.  **六种有效思考模式与注册式确定性协议投影**：

    - 文档、配置与代码全栈统一为六种有效思考模式：**none（无可控制思考）、toggle（二元开关型）、fixed_on（固定开启型）、discrete（离散档位型）、budget（Token 预算型）、adaptive（模型自适应型）**，并配对五种原生投影动作：**omit、send_level、send_budget、send_toggle、send_adaptive**。

    - 协议适配层（thinking_projection.py）通过已注册的投影策略表将冻结的 EffectiveThinkingSemantics 确定性投影为 Gemini Native（thinkingLevel / thinkingBudget）、Responses / Chat Completions（reasoning.effort / reasoning_effort / thinking）、Claude Messages（thinking.budget_tokens / adaptive + output_config.effort）及 Moonshot/Kimi（thinking.type="enabled"）等原生 Wire 参数，**Adapter 严禁直接读取调用方原始 think_level 或自行决定档位转换**。

| **语义层次**                | **核心契约结构与关键属性**                                                                                                                | **架构职责与边界约束**                                                                                                                 |
|-----------------------------|-------------------------------------------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------|
| **requested（请求意图层）** | requested_raw、requested_intent（七态）、source（omitted / explicit_empty / caller_explicit）、policy（prefer / require）                 | 忠实记录调用方原始意图与参数来源；原始标签与来源状态仅用于快照审计与问题溯源，不直接决定 Wire 形状或请求身份                           |
| **effective（有效执行层）** | EffectiveThinkingSemantics（mode、wire_action、wire_strategy、level、budget_tokens）+ satisfies_require                                   | 由执行面依据冻结映射合同唯一裁决；驱动采样参数联合校验、协同 policy 决定 request_hash 幂等身份，并作为 Adapter 原生投影的唯一只读输入  |
| **observed（上游观测层）**  | ObservedThinkingEvidence（reasoning_tokens、has_explicit_thought_blocks、thinking_parameter_stripped、thinking_degraded、effective_mode） | 由协议适配层从原生响应体或受控网关响应头提取，随 execution-fact.json 归档，供 ResultCommitter 在正常提交与只提交恢复时执行后置合同核验 |

4.  **思考激活态下的采样与输出参数交叉校验**：

    - 当 effective 语义判定本轮思考处于激活态时，语义解析器自动联动核验合同声明的激活态约束（when_thinking_active）：包括**禁用自定义温度（forbid_temperature）、禁用 top_p（forbid_top_p）、强制固定温度值以及要求思考预算严格小于最大输出上限（budget_lt_max_output）**。

    - 若调用方未显式传入被禁用的采样参数，Relay 自动剔除合同默认采样值并保持省略，绝不主动补写引发冲突的默认值；若调用方显式传入了冲突参数，则在推理发送前直接按 OPTIONS_THINKING_CONFLICT 拒绝。

5.  **存量冻结 Session 与历史 Request 的无损兼容边界**：

    - 已创建的旧格式冻结 Session 通过内存兼容转换器在不改写数据库快照的前提下，按原认证映射规则动态提升为七态映射结构（旧合同未声明的普通深度偏好仅允许在 prefer 下回退至已声明的 AUTO 执行且标记 satisfies_require=False，OFF 绝不适用该回退）；已受理的历史 Request 则完全保留其原始快照、历史身份哈希算法与原有投影路径，确保存量恢复零中断。

协议能力以 Protocol Profile 为稳定复用单元，模型与渠道的绑定关系由配置治理控制面维护，不在架构层为每个模型复制一套 Adapter 说明。核心 Protocol Profile 建议保持如下边界：

- Responses Profile：适用于通过兼容性验证的 Model Supply；负责 response / output 结构、可恢复 response identity、reasoning transport、Structured Output wire projection 与对应错误语义，不因为模型名称不同复制协议实现。

- Chat Completions Profile：适用于经过验证的 messages 型兼容供应；负责 role / content、采样参数、response_format、tool / multimodal 等已声明能力的显式投影，不假设 Responses 特有的 response.output 或续接语义。

- Claude Messages Profile：负责 Claude Messages 的 content blocks、thinking / history、tool 与 Structured Output 相关原生语义；模型间差异由 Capability Contract 约束，不通过新增模型专属执行分支解决。

- Gemini Native Profile：负责 contents / history、fileData / fileUri、Gemini-facing Provider Projection、thinking 与 responseSchema projection；External URL / Files API 等 Material transport 仍是 Binding 层决策，不改变 inference route。

Model Supply 只引用已经注册并通过兼容性验证的 Protocol Profile。一个 Protocol Profile 可以服务多个模型和多个渠道；一个模型也可以存在多个使用不同渠道或不同已批准协议的 Model Supply，但 Session 一旦冻结具体供应关系，后续 Request 不在协议之间静默漂移。

API 与 Worker 共用上述自动配置处理流程，消费同一配置版本的不可变运行快照，并校验配置身份一致性。**API 与 Worker 彻底消除按 Gemini、Moonshot 等渠道手工注册推理、文件与缓存适配器的启动分支，统一收敛至公共协议与资源装配工厂（protocol_adapters/factory.py 与 control_plane/resource_registry.py）**。公共工厂遍历激活快照中的 ConnectionSpec 与关联供应合同，按 connection_id 动态构建并隔离**推理协议适配器、Provider 原生文件适配器、显式缓存资源适配器、Token 计数器及底层 SDK 客户端**。同一 connection_id 内部安全共享受控连接池，不同账号或端点连接严格隔离实例并由工厂统一管理优雅关闭（Graceful Shutdown）。当某个连接的服务端密钥未配置时，工厂仅跳过该连接的运行实例构建而不中断服务启动；当请求实际路由至未配置连接或 Worker 节点发现自身加载的连接实例指纹（connection_spec_hash）与 Request 冻结合同不匹配时，在 not_sent 阶段按 Fail-Closed 明确报错（如 ROUTE_CONNECTION_NOT_CONFIGURED），绝不用其他渠道连接或本地默认值静默顶替。

RouteCatalog 提供默认 revision；API /health 返回当前 route_revision，API 与 Worker 启动日志都记录 route_revision/catalog_hash、已注册 Inference/File connections。

## 10.2 缓存能力接入边界

**缓存能力按具体供应关系认证，不按厂商、协议家族或模型名称直接继承。**同一协议可以服务具有不同缓存能力的模型；同一模型通过不同渠道、账号或区域提供时，也可能具有不同的可执行合同。

Protocol Profile 提供可复用的协议表达，Adapter 消费已经确定的物理绑定并完成原生投影。编译器只引用已登记、已验证的协议能力，不从用户配置生成字段路径、资源接口或模型专属适配器。缓存机制、认证条件及生命周期统一见第 11 章。

| **实现备注｜兼容窗口：**SessionCreateRequest、SessionRequestCreate、MaterialCreateJSON 仍接受 legacy connection_id / target_connection_id。默认 ROUTE_LEGACY_HINT_MODE=warn 时 mismatch 只记录并忽略；Dify 侧彻底清理旧字段后可切 strict，并进一步把 schema 收紧为 forbid。当前“客户端不再拥有路由权”已落实，“公开 schema 完全删除旧字段”尚未完成。 |
|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<thead>
<tr class="header">
<th><p><strong>配置一致性同时包含合同一致和执行代码兼容，配置摘要相同并不足以证明节点能够执行该合同。</strong>API、Worker、路由与执行节点在受理和领取任务前，应确认所需快照、协议实现及运行时能力均已具备。无法识别或不兼容的执行合同不得用本地默认值补齐，也不得按另一份配置重新解释。</p>
<p>新建 Session 使用当前获批配置；既有 Session 的后续 Request 继续依据该会话冻结的供应、能力和策略形成执行身份。请求恢复使用自身已经保存的事实，不重新读取当前表格决定执行行为。</p>
<p>协议配置一经发布即保持不可变。协议行为变化应形成新的配置身份并声明兼容条件；旧快照及对应执行实现，应保留至依赖它们的会话、在途请求和资源引用全部退出。退役检查同时覆盖配置与执行代码，不能只保留一份已经无法执行的历史配置。</p>
<p>发布与回滚通过原子切换改变后续新会话的受理依据，不改写既有会话和在途请求。控制面暂时不可用时，可以在策略允许的期限内使用最后一个已验证且仍获授权的快照；安全撤销可以阻止新的受理或执行，但不得改写已经保存的请求、结果和错误事实。</p></th>
</tr>
</thead>
<tbody>
</tbody>
</table>

| **源码依据**：**app/control_plane/snapshot.py**；**app/control_plane/catalog.py**；**app/protocol_adapters/base.py**；**app/protocol_adapters/registry.py**；**app/protocol_adapters/profiles/**\*；**app/protocol_adapters/wire_projection/**\*；app/materials/provider_files/\*；app/api.py；app/worker.py |
|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# 11. 缓存执行控制面与上下文复用

## 11.1 缓存设计定位与总体执行架构

缓存是统一执行控制面中的上下文复用能力。它不替代会话历史、材料事实、请求幂等或业务结果存储，也不赋予执行器修改调用目标的权限。**缓存复用沿用整体架构的四层分工。**调用方表达本轮缓存意图。**控制面**按具体供应关系认证缓存能力，定义可执行合同与策略。**执行面**在会话冻结的供应与合同范围内，结合本轮上下文形成可重放的缓存计划。**运行时**在材料表示确定后落实计划，管理缓存资源、物理绑定和发送前封存。**协议适配层**依据协议合同，将已封存的执行事实投影为 Provider 原生请求。

Atomic Dispatch Seal 封存的是最终 CacheExecutionBinding、资源 pin 与本次 Adapter 执行输入事实，并形成首次 Provider dispatch 的受控门槛；它不应被描述为已经天然等价于最终 HTTP body 的字节级签名。Usage / Audit 只记录 observed execution，不反向改写 requested / effective 语义。

## 11.2 缓存能力治理与供应资格

缓存能力由模型潜在能力、渠道与账号实际能力、协议表达、运行时实现及安全策略共同约束，最终归属于具体供应关系。官方文档、兼容接口或用量字段只能提供候选依据，不能直接证明某个渠道具备可控缓存能力。

会话创建时，可以声明“必须具备已认证缓存能力”这一高层资格约束，用于筛选供应；它不携带机制、资源有效期或句柄，也不代替每轮请求的上下文适用性判断。后续请求要求强制缓存，而冻结供应不具备合格能力时，应在推理前拒绝，不能重新选择渠道。

**最终可用的缓存能力必须由“模型潜在能力 ∩ 渠道/账号/区域实际能力 ∩ Protocol Profile 可表达能力 ∩ 运行时能力 ∩ 安全/租户策略”求交，并归属于具体 Model Supply，而不是单纯归属于 Model Catalog。**

缓存能力作为 Capability Contract 的独立子合同治理。Capability Contract 的缓存子合同声明该 Model Supply 可能支持的机制、门槛、可缓存块类型、参数依赖和 usage 语义；Protocol Profile 的缓存部分声明这些机制如何映射到字段路径、资源 API、断点位置、TTL 约束和 usage mapping；Cache Policy 声明机制优先级、作用域、预算、共享边界、刷新/清理与允许的降级规则。**三者职责不能压缩成一个 supports_cache=true。**

每一种缓存机制都必须按具体 Supply 独立认证。能读取 cached_tokens 不等于已经认证了可控缓存能力，协议看起来兼容也不等于可以注入所有缓存字段；模型文档宣称支持也不能自动推导聚合渠道、账号或区域具备同样能力。未认证机制不得进入 on 的可用机制集合；auto 可以按策略得到 None，但不得伪造“已启用缓存”。

同一模型存在多个渠道供应关系时，调用方默认只选择模型与缓存意图，不选择具体 Supply 或物理缓存机制；Relay 先做资格过滤，再按 Route Policy 选择并冻结 Model Supply，随后只在该冻结供应关系内部解析 effective_cache_mechanism。缓存决策不得为了提高命中率突破 Session 已冻结的渠道、账号、区域或协议边界。**供应具备缓存资格与本轮上下文适用，是两个独立判断。**控制面按具体供应关系认证缓存能力并发布适用策略。执行面在创建会话时按资格约束筛选并冻结供应，在受理每轮请求时再依据冻结合同和本轮上下文判断适用性，形成缓存计划。

## **11.3 三类物理机制与双轨协同原则**

Relay 以显式资源、断点和隐式前缀三类物理机制表达缓存，并将其纳入同一规划与治理体系。

显式资源型缓存绑定内容身份不可变的资源及其确切代次，生命周期受供应合同约束。断点型缓存通过合法内容块与语义锚点声明复用边界，遵守协议规定的顺序、数量及预算限制。隐式前缀缓存依靠稳定上下文布局和经过认证的协议提示争取复用机会，不持有提供方内部缓存对象，也不承诺其保留或命中。

**底座轨负责通用隐式前缀治理，增强轨按需采用显式资源或断点。**各协议共享规范化、前缀比对与观测能力，具体差异由供应合同和协议规则表达。增强轨内部选择一种合格机制；底座轨是否实际启用，仍须满足本轮意图、能力认证和上下文条件。

**双轨协同服从经过认证的组合与互斥矩阵。**Gemini 的显式资源与隐式前缀按互斥关系裁决；Claude 只有在供应与协议均授权分层、两轨均适用且覆盖包含关系成立时，才能形成组合绑定。仅具备隐式能力的供应使用底座轨。

逻辑上允许组合，不代表上游必然产生两类命中或双重折扣。实际效果由响应证据确认，协议名称相同也不构成能力继承依据。

## 11.4 缓存意图、适用性与决策规则

off 表示本轮不主动创建、引用、声明或刷新 Relay 可控缓存；auto 按冻结策略选择合格机制或不启用；on 要求在供应具备合格能力且本轮上下文适用时启用缓存。默认值解析遵循第 9.2 节。

requested_cache_mode 属于每轮 Request，仅表达 off、auto、on 三种缓存意图。**Session 冻结供应关系及其缓存可执行合同，不冻结后续每轮请求的缓存选择。**请求省略该字段时，采用会话已经冻结的默认策略；未采用供应级默认策略的入口继续遵守原有平台默认行为。

**省略字段、显式 auto 和显式 off 必须分别识别。**默认策略不能覆盖调用方明确提交的选择。治理策略禁止强制缓存时，应拒绝显式 on；默认强制也必须同时满足强制授权、能力认证和安全条件。配置更新不改变既有会话已经冻结的默认行为。

请求归一化保留字段是否显式提供、采用的冻结默认及最终解析结果，并按其对执行语义和恢复边界的影响形成幂等身份。不得仅因最终没有启用缓存，就把不同缓存意图视为同一请求。

调用事实区分 requested、effective 与 observed 三层。requested_cache_mode 只有 off / auto / on：off 表示 Relay 不主动创建、引用、声明或为本次调用刷新可控缓存，但不承诺关闭 Provider 自主透明缓存；auto 由 Relay 按冻结策略选择合格机制或 None；on 要求 Relay 在存在合格机制且本次上下文适用时启用缓存。**on 若仅因可缓存上下文未达到门槛，必须 Fail-Safe 为 None 并继续普通模型调用；只有 Supply 无可用机制、协议未认证、能力不可表达或安全策略禁止等合同不满足时才 Fail-Closed。**普通资源创建失败、网络错误、配额耗尽或缓存引用失效不等同于“低于门槛”，不得借 Fail-Safe 静默换机制或把 on 改成 None。

若后续 Request 提出的硬能力要求超出 Session 已冻结的 Capability Contract，或所要求的缓存能力在该 Supply / Protocol Profile 上不存在、未认证或被安全策略禁止，应在 Provider dispatch 前 Fail-Closed，或由调用方显式创建新的 Session；不得为了缓存静默换渠道、改协议或降低硬要求。**缓存 on 的上下文门槛不足属于“本次不适用”，不是“供应能力不存在”**：只要 Supply 本身具备合格缓存机制，Relay 应将该次 effective_cache_mechanism 确定性降为 None，并继续同一上下文的普通模型调用。

**具有缓存能力与本轮能够启用缓存是两个独立判断。**供应能力不存在、协议未经认证或安全策略禁止时，强制请求应拒绝执行；供应能力合格，但能够确定本轮上下文低于最小门槛时，应不启用缓存，并继续同一上下文的普通推理。

门槛判断采用与实际协议和材料表示一致的计数或可靠评估。未知不能当作零，也不能通过标记“不适用”绕过必要判断。提供方返回的门槛错误，由经过认证的协议错误分类识别；鉴权、配额、网络及其他错误不能冒充短上下文。

自动模式只有在冻结策略允许、推理尚未发送且上下文能够完整恢复时，才可因准备失败转为无缓存执行。强制模式的其他准备失败应明确报错。**错误识别属于协议职责，是否允许继续属于策略与执行编排职责，Adapter 不得自行删除缓存引用并重发。**

think_level、options 与 requested_cache_mode 都是调用意图，不要求调用方维护每个模型真实支持的思考档位、参数范围、缓存机制或冲突矩阵。Relay 以冻结的 Capability Contract、Cache Policy 与 Protocol Profile 为权威，将 requested intent 解析为 effective semantics：无法满足的硬要求在 dispatch 前 Fail-Closed；**唯独缓存 on 在 Supply 本身存在合格机制、但本次上下文明确未达到缓存门槛时，确定性降级为 effective_cache_mechanism=None，并继续同一模型调用。**业务 Workflow 不根据不同模型拼 generationConfig、reasoning_effort、thinking、max_tokens、prompt_cache_key、cache_control 或其他 Provider Native 字段。

## 11.5 ContextPlan、内容身份与 Material 边界

ContextPlan 保存指令、历史、材料身份及其必要的表示规划。前缀或资源内容身份覆盖完整内容、顺序、材料表示、相关参数和授权作用域，不能仅以模型名称或会话当前缓存指针代替。影响上下文语义的内容变化，应形成新的内容身份与绑定。

**缓存优化遵循调用方零侵入、语义不变的最大合法复用原则。**调用方无需为缓存拆分 Session、改写 Prompt 或维护提供方字段。Relay 在冻结合同、隔离、预算和成本约束内，寻找能够证明等价的最大合法复用边界，不以命中率替代语义正确性。

语义等价必须同时保持：**消息角色与轮次拓扑不变、单消息内多模态 Part 的顺序及关联不变、业务正文与有效约束不变、材料实体真值不变。**允许的受控变换限于序列化规范化、清除 Relay 自行注入的非业务易变内容，以及历史冻结。结构化对象与合同声明的无序集合可以确定性规范化；业务字符串、代码缩进及有序内容不得因统一空白或排序而改变。

已提交历史应作为可重放的只读前缀，不因后续阶段、思考档位或采样参数重新生成历史正文。Relay 自有追踪信息应置于带外或预先定义的动态槽位，不得污染稳定前缀，也不得按关键词删除调用方正文。**单轮请求的思考控制语义与缓存内容身份严格正交分离**：单轮思考档位、思考预算及约束策略仅参与当轮 Request 的逻辑执行身份（request_hash）与推理报文投影，**绝不进入 ContextPlan 语义身份指纹、分段链式物理前缀指纹（cache_fingerprint.py）以及显式缓存资源（如 Gemini cachedContents）的内容指纹与创建载荷**；同时，显式缓存创建边界（gemini_stateful_cache.py）强制拒绝混入单轮思考或采样生成配置。因此，同一 Session 在多轮对话或多阶段工作流中动态切换 LOW、HIGH、XHIGH、MAX 等不同思考档位时，底层已提交历史前缀与显式缓存的内容指纹保持完全稳定，不会因思考深度调整而触发缓存失效或重复物化。

语义身份指纹与分段链式物理指纹分工独立。前者描述受控规范化后的逻辑内容及必要身份，不依赖临时资源句柄或单轮思考/采样控制参数；后者描述实际投影的分段内容、顺序和表示，用于确定最长公共前缀及首个分歧位置。业务内容变化与传输表示变化应分别诊断。指纹一致只能作为复用判断的证据，不能覆盖授权、作用域、资源有效性及协议限制。

**语义身份指纹与分段链式物理指纹分工独立。**前者描述受控规范化后的逻辑内容及必要身份，不依赖临时资源句柄；后者描述实际投影的分段内容、顺序和表示，用于确定最长公共前缀及首个分歧位置。业务内容变化与传输表示变化应分别诊断。指纹一致只能作为复用判断的证据，不能覆盖授权、作用域、资源有效性及协议限制。

**Material Binding 回答材料如何到达上游，Cache Binding 回答已确定上下文如何复用，两者不能混同。**缓存引用不能改变材料身份、真实内容或已经冻结的材料表示。

**Material Binding ≠ Cache Binding。**Material Binding 回答“这份 Canonical Material 以什么 Provider 可消费的表示到达上游”；Cache Binding 回答“本次 Request 如何复用一段已经确定的上下文”。Provider file ID、fileUri、Signed URL 或 extraction artifact 不是缓存资源；Stateful Cache Resource 即使引用了某个 Material 的 Provider 表示，也不得反向改变 material_id、真实 MIME、文件名、内容 hash、来源语义或已冻结的 material binding generation。

缓存计划只能引用经过验证的 Material 身份与表示。如果某种缓存机制要求特定 transport 或 projection，该要求必须进入受理/执行规划并在首次 Provider dispatch 前形成明确的物理执行事实；不得在 Adapter 内因为缓存命中、资源过期或 Provider 错误临时切换 Material Binding，并伪装成原 Request 的普通恢复。

显式缓存的材料准入预算（materials/cache_selector.py）与冻结缓存子合同的 **explicit_cache_material_mode（inline_bytes_only vs file_uri_allowed）** 深度协同。对于受内联体积限制的缓存投影（inline_bytes_only 模式），采用分级、确定性的材料准入策略，默认内联安全预算为 **60 MiB**，按实际缓存表示的编码前内联字节计量（结构化文本桥接按派生产物字节计量），为编码膨胀、指令、历史和协议封装预留空间。**而当供应合同认证并启用 file_uri_allowed 模式（如 Google 官方 Gemini Supply），且候选材料在当前连接与 account_scope_id 下已绑定（或规划绑定）为 gemini_file_uri 时，该材料通过轻量级 fileData(fileUri, mimeType) 引用结构进入 cachedContents，不再向缓存创建报文内联原始文件字节；此时准入选择器（cache_selector.py）仅按固定 URI 引用元数据封装开销计量其内联占用，从而豁免 60 MiB 内联字节预算对大体积官方 fileUri 材料的裁剪，仅受官方 Files API 自身的单文件与总上下文 Token 上限约束**。

通过语义与协议校验的候选材料，依次考虑：纯文本、JSON 与 DOCX 结构化文本投影；不超过 15 MiB 的 PDF；较大 PDF 与原生 DOCX；视频；图片及其他材料。同级候选结合预计 Token 字节密度、体积与稳定身份确定顺序。密度用于规划估计，不作为实际 Token 用量或收益证据。

**选择优先级只决定缓存准入，不改变模型输入顺序。**Session 级稳定材料仅能在证明等价的既定槽位中编排，消息内附件不得为了装箱跨越其关联文本。超出预算的材料退出本次缓存集合后，仍须完整保留在正常推理输入中。

因此，AIHubMix Gemini 供应受 inline_bytes_only 合同约束，显式缓存资源仅使用 Relay 持有的内联材料字节创建；Google 官方 Gemini 供应则在 file_uri_allowed 合同授权下直接复用同一连接作用域内的官方 fileUri 构建显式缓存。在任何模式下，未进入显式缓存集合的材料均继续遵循已经确定的 Material Binding 完整保留在正常推理输入中；退出缓存绝不等于退出上下文，缓存预算不得成为删除附件、裁剪内容或改变材料身份的理由。

**DOCX 原文件与合同授权的结构化文本投影均作为一等材料参与规划。**结构化投影应保留合同承诺的正文、标题层级、表格结构、段落顺序和内容关联；源材料身份、真实 MIME、来源与内容摘要保持独立且不可变。

**桥接必须以完整性校验作为准入条件，禁止静默跳过无法解释的业务节点。**表格中的内容控件、修订包装及其他未获认证的结构，不能通过仅遍历直接单元格而被遗漏。图片、公式或嵌入对象等内容超出桥接合同能力时，同样应拒绝该投影。只有合同明确授权时才可采用原生 DOCX 表示；不存在合法表示时，必须在模型发送前明确报错。

派生产物身份应覆盖源内容、授权作用域、投影规则、解析实现身份及产物摘要，防止复用不符合完整性要求的旧产物。已冻结请求的恢复继续验证并消费原绑定，不得静默重提取并替换其执行输入。

- 附件属于原始上下文语义的一部分。对于 Gemini User Content，消息角色、Part 顺序、文本与附件之间的关联关系、模态信息及其内容完整性，均属于必须保持的上下文语义。当会话启用原生历史材料自动去重后，会话锚定材料唯一且稳定地驻留于首次提交的原生历史消息中，后续轮次仅在对话尾部追加增量轮次，使得 **已提交的原生历史序列（contents\[0..2k-1\]）天然构成严格单调增长、字节级不可变的稳定前缀（Stable Prefix）**。 基于这一结构特性，物理缓存规划直接收敛为 **“原生历史前缀切片（Native Prefix Slicing）”**：

1.  **严禁跨消息拆解附件**：系统彻底摒弃从历史或当轮 User Content 内部剥离材料 Part 另建合成缓存前缀的做法，显式缓存与隐式前缀边界严格对齐 **完整的已提交 User/Model 消息轮次边界**，100% 保持原始图文分组与轮次拓扑不变；

2.  **供应合同授权匹配**：若历史切片中包含原生文件引用（fileData），仅在冻结缓存子合同明确授权 explicit_cache_material_mode = file_uri_allowed 时允许直接构建显式缓存，未授权内联转换的供应则安全走隐式前缀复用或完整无缓存推理，绝不强行改写历史文件表示；

3.  **缓存关闭（requested_cache_mode=off）轻量旁路**：当本轮缓存意图为 off 时，运行时跳过内联缓存材料字节读取、候选装箱枚举与合成前缀物化，直接走轻量无缓存路径（bypass_off）并仅保留必要的物理 Wire 摘要审计，消除无谓的 CPU、内存与存储读取开销。 缓存优化不能以牺牲上下文语义完整性为代价。

ContextPlan 与请求身份的装配边界参见第 3.2 节；Material 的长期身份和 Provider 传输资源说明参见第 5.1～5.3 节。此处以内容完整性、材料表示及缓存隔离为讨论重点。

## 11.6 逻辑计划、物理绑定与 Native Wire 投影

缓存执行遵循：**先完成条件筛选，再生成统一缓存计划，集中执行组合与互斥仲裁，最后落实选定分支。**逻辑计划与实际执行绑定分别冻结，共同维持请求可重放性。

条件筛选依据同一冻结上下文，检查缓存意图、供应与协议认证、安全隔离、稳定前缀及已知门槛，记录合格候选、排除原因和待核验条件。筛选阶段不为每条轨道生成独立的完整计划，也不创建缓存资源。

随后只生成一份 CacheIntentPlan，完整表达合法候选的配置与覆盖边界、组合或互斥关系、成本依据、运行时待决检查以及允许的准备分支，并形成整体校验摘要。**计划完整，执行分支可以待决；未获授权的分支不得在运行时补入。**

材料绑定确定后，运行时先核验计划与上下文身份，完成只读资源检查和必要计量，再集中仲裁。评估阶段不领取创建操作、不创建 Provider 缓存，也不发送探测性推理。某一候选条件未获确认，不应阻断另一条已经授权且核验通过的合法分支。

仲裁完成后，只有被选分支进入资源创建或绑定落实，随后复核实际状态，形成 CacheExecutionBinding，记录有效轨道、覆盖关系及裁决依据。准备失败只能进入原计划授权的分支；鉴权失败、未知创建副作用和执行权校验失败不得成为切换机制的授权。

**计划格式、解释规则和校验摘要随 Request 冻结。**幂等重放及恢复沿用原计划；进入首次模型发送门槛后，禁止换机制、换渠道或再次推理。

协议适配层验证并消费已封存的绑定，依据协议合同生成 Provider 原生请求。适配器不得重新解释缓存策略、选择供应或根据 Provider 错误切换缓存机制。调用方参数、业务元数据、原生缓存字段与资源句柄均须经过各自的校验和投影边界，禁止直接合并到上游请求。

**当最终 effective_cache_mechanism=None 时，Adapter 必须从同一 ContextPlan / 物理上下文计划投影完整 uncached context。**不能通过简单删除 cachedContent、cache_control 或 prompt cache hint，就遗漏原本由缓存资源承载的 history、instructions、Material 或其他上下文。缓存启用与否只能改变复用方式，不能改变业务上下文本身。

Adapter 投影必须保持上下文完整。准备阶段依法转为无缓存执行时，应从同一逻辑计划恢复完整指令、历史和材料，不能简单删除缓存字段后丢失由资源承载的内容。隐式前缀的内容、顺序或绑定摘要不一致时，应在首次推理前拒绝。

运行时的发送前原子封存（AtomicDispatchSealer）将 **前置材料不变量阻断、本轮与历史材料双重物理核验、缓存执行绑定、统一资源引用保护（pin）及执行权校验（ExecutionFence）** 严密收口于 **not_sent 阶段** 的同一数据库事务边界内：在推进不可逆派发状态前，运行时首先执行 **发送前材料不变量核验（verify_pre_seal_material_invariants）**，确保冻结历史分段摘要未变、当轮绑定序列与 current_effective_material_ids 严格一致、任何判定为历史继承的自动材料在当轮新增 Part 中出现次数严格为零、且全部历史材料发生均已完成有效性核验，从而 **彻底杜绝因内部上下文校验失败而跨过派发门槛掉入 dispatch_started / indeterminate 的风险**。随后在数据库原子封存事务中，按稳定顺序锁定全部本轮与历史关联的 Canonical Material 及 Provider Binding，复核租户、会话、connection_id、账号作用域、状态合同、绑定代次（generation）、URI 与安全有效期门槛，**同时为本轮绑定材料与历史继承材料建立受数据库触发器保护的活跃资源 pin（正常提交释放，indeterminate 或无法证明上游已停止读取的取消转为 held 持续锁定保护）**，记录封存期材料发生快照并推进 provider_dispatch_state = dispatch_started，且在事务末端再次对照数据库壁钟（clock_timestamp()）复核执行权。封存摘要（seal_hash）完整覆盖冻结请求快照、Session、历史、本轮材料绑定、**本轮投影材料发生列表、历史锁定绑定引用**、Owner、ContextPlan、CacheExecutionBinding、ExecutionFence、完整物理上下文计划（PhysicalContextPlan）及亲和请求头；它是对协议适配层执行输入的原子授权与完整性记录，协议适配层只读消费封存包且在遇到未知或损坏的历史条目结构时严格执行 Fail-Closed。

**协议适配层在入口处强制重算物理计划内容摘要并复核整体封存摘要，任何针对 Session、材料 ID、亲和头、ExecutionFence、ContextPlan 或缓存绑定的篡改均在发包前被拒绝；文本材料与文档投影严格消费已冻结的对象引用及内容摘要，严禁在适配层投影时改读未冻结的最新 Material 字节。**端到端跨层执行顺序固定为：

- **1. 执行面**加载已冻结的 **FrozenExecutionPlan**（若已处于 **result_stored** 则直接跳至第 4 步）；

- **2. 运行时**核验 **ExecutionFence** → 落实并安装 **Material Binding** → 基于 **CacheIntentPlan** 完成只读评估与集中成本仲裁（**实时经济性观测仅更新运行时计划副本，不回写冻结计划**） → 物化并安装 **CacheExecutionBinding** → 事务性建立 pin、生成 **SealedDispatchBundle** 并写入 **dispatch_started** → 通过 **ControlledInvoker** 单次调用**协议适配层**；

- **3. 协议适配层**核验封存包 → 投影 **Native Wire** → 经底层传输围栏（**TransportGuard**）再次核验活跃执行权后发送单次 HTTP 请求 → 解析并返回 **StandardizedProviderResult**，由**运行时**落盘 raw-response.json、response-output.json 与 execution-fact.json 并写入 **result_stored**；

- **4. 执行面**通过 **ResultCommitter** 消费 **ActualExecutionFact** 完成 Canonical 后置校验、上游执行授权核验与 Session/Request/Job 终态原子提交。

## 11.7 缓存资源生命周期、双 Fencing 与故障恢复

**会话粘性（Session Affinity）是由受认证 ProtocolProfile 条件化驱动的作用域机制**（runtime/affinity.py）。仅当冻结的 ProtocolProfile 显式声明启用亲和策略（affinity_policy.enabled = true）且目标请求端点匹配受控主机白名单（allowed_hosts）时，Relay 才在冻结供应、连接、账号与区域边界内维护会话亲和标识及其代次（Generation），并保证同一代次内的缓存资源操作与模型调用注入一致的亲和头或协议提示。**对于无需网关粘性的第一方官方直连供应（如 Google 官方 Gemini Supply），Profile 声明禁用网关亲和头，运行时亲和模块自动作为空操作（No-op）安全旁路，既不向官方端点注入额外亲和头，也不触发基于粘性丢失的代次淘汰**。

粘性提示不保证上游始终使用同一实例或账号，也不构成命中证明。漂移判断应结合合同认可的粘性失效信号、有效期内资源可见性异常，以及稳定长前缀上的连续真实零命中；缺失用量、单次零命中和普通错误不得直接等同于漂移。

**漂移淘汰只能在首次模型发送前、受执行权保护的窗口内完成。**需要推进亲和代次时，应停止旧作用域资源接受新引用，并使新绑定使用一致的作用域事实；既有在途引用按其冻结事实收敛。亲和调整不授权改变冻结供应，也不授权重发已经发送的推理。

**缓存资源副作用与模型推理副作用分域管理。**模型尚未发送，不代表缓存资源一定没有创建。资源创建结果未知时，应先记录并对账，不能盲目重复创建。

缓存引入的是第二套独立副作用状态机，与模型推理状态机分域治理。**运行时**中的缓存操作（cache operation / resource / binding / pin）负责缓存资源生命周期，Request **ExecutionFence** 负责模型推理执行权；两者实施**双重围栏（Dual Fencing）联动校验**：缓存 Operation 账本显式记录所属 Request 的 owner 与 epoch，在启动创建、观测、验证、结束及发布资源前，必须同时校验所属 Request 围栏有效以及 Operation 自身的 owner / epoch / state / lease 有效——**一旦所属 Request 执行权失效，即使 Operation 自身租约尚未到期，也严禁发布缓存资源**。provider_dispatch_state 继续专门描述模型推理派发阶段：**not_sent → dispatch_started → result_stored → committed**。

not_sent 只证明模型推理尚未发送，不证明远端缓存资源创建一定没有发生。对于显式缓存创建出现 **unknown** 或已失租 **running** 的窗口，**严禁重新发起 POST 创建**。系统仅提供有条件的精确对账恢复：**只有当操作账本已持久化精确、合法的远端缓存句柄（如 cachedContents/...）时，才允许同一 Session 的新有效执行权领取新的对账 operation_epoch，通过只读 GET 验证原句柄在远端的真实有效性与内容身份，核实无误后发布资源并保留完整对账审计链；若未保存精确句柄、GET 无法证明有效或返回身份不一致，则继续保持 Fail-Closed，绝不猜测资源名、不扫描远端列表、也不盲目重建。**

首次推理封存前，只有在确认不存在未知外部创建副作用的前提下，才可在原冻结供应、相同授权作用域和相同逻辑规范下复用或物化资源。**一旦跨过 Atomic Dispatch Seal 进入 dispatch_started，即形成不可逆派发门槛：不得因上游返回缓存 4xx、资源过期、未命中、准备差异或存在备用渠道而“去缓存重发”、换机制、换渠道或再次推理。进入 result_stored 后的恢复完全收口在执行面，只加载已归档事实完成结果/历史提交与 pin 释放，绝不重新进入运行时读取、续期、重建缓存资源，也不重新调用协议适配层。**

资源操作与请求执行分别受执行权校验保护，过期执行者不能发布资源、替换绑定或覆盖结果。显式资源通过请求引用保护避免被提前回收；资源退役先停止接受新引用，再等待在途引用退出。

**首轮即判断复用潜力，不以已有历史或现成句柄作为规划前提。**当稳定指令或材料达到认证门槛、存在合理复用预期且满足策略与成本条件时，可在首次模型发送前准备选定缓存资源，或随真实业务调用设置合法断点和稳定前缀。不得通过额外空跑模型建立缓存，也不得承诺后续请求必然命中。

**滚动快照采用前缀一致性、增长迟滞与经济收益联合门控。**只要旧资源仍覆盖合法稳定前缀，就优先复用旧快照并发送完整增量后缀；常规重建须同时满足前缀匹配、相对增长和最小增量要求，以及净收益条件，避免每轮重复写入。合同规定的未缓存增量上限亦须进入统一评估。

成本比较应使用冻结价格依据，考虑读取、写入、存储时长、资源重叠和预计后续复用次数。**预计收益与实际费用分别记录，未知成本不得按零处理。**策略另行授权的有界冷启动试用必须标明试用性质及预算，不能记为已经证实的节省。

## 11.8 缓存安全隔离、Usage 与观测审计

Relay 管理、规划和引用的缓存资源必须按租户、Session、供应、账号、区域、协议及完整内容身份共同隔离，其中 **session_id 是不可省略的硬作用域**。

**不考虑跨 Session 内容复用。** 即使不同 Session 的上下文、附件及内容指纹完全相同，也不得共享、继承或引用彼此的 Relay 管理缓存资源。内容身份相同不能覆盖 Session 隔离边界。

缓存复用仅限同一 Session 内，并继续满足其他授权作用域、供应资格和内容兼容性条件。

提供方内部透明缓存属于 Relay 不可控制的 observed fact，不构成 Relay 管理的跨 Session 资源复用能力，也不得作为放宽 Session 隔离边界的依据。

off 不等于立即删除既有资源，更不等于零保留保证。材料撤权应使相关缓存失效；缓存回收本身不能改写会话历史或材料事实。

缓存资源沿用同一 Owner 边界，并额外受 session、Model Supply、account / channel / region、Protocol Profile 与 content fingerprint 约束。Provider cache handle 属于**受控执行事实**，只保存在资源/绑定对象中，不进入普通 API、业务 metadata 或常规日志；tenant、conversation、account、channel 或 region 任一安全边界变化都不得继承原 handle。

活跃 Request 通过 pin 阻止资源被清理；retiring 先禁止新 pin，再等待在途引用退出。requested_cache_mode=off 只表示本次不主动创建、引用、声明或刷新 Relay 可控缓存，**不等于删除既有资源，也不等于零保留策略，更不保证关闭 Provider 自主透明缓存。**需要零保留、区域限制或 Provider 侧缓存禁用保证时，应由安全与部署策略选择满足要求的 Supply / account / project 边界，而不是把 off 当作数据保留开关。

**缓存事实持续区分调用意图、实际执行与上游观测。**实际执行记录启用轨道、覆盖边界及仲裁依据；上游观测记录能够被响应证明的读取、写入和未缓存用量。组合计划不等于组合已执行，组合已执行也不等于两类命中均已发生。off 或未主动启用缓存时观察到的透明命中，仍只属于观测事实。

**真实零值、未知、未提供与未启用必须分别记录。**隐式轨只有在适用条件经过认证、稳定布局实际进入原生请求且适配器消费了执行绑定时，才能记录为有效机制；一次零命中不足以否定该机制。

Claude 原生用量中的 cache_read_input_tokens 与 cache_creation_input_tokens 分别作为缓存读取和写入证据；input_tokens 表示未缓存输入。三项均有效时，完整输入量按**未缓存输入＋缓存读取＋缓存写入**计算，命中比例使用该总量作为分母。证据不完整时比例保持未知，原始计数和原生响应继续保留。

**报文诊断位于 HTTP 传输层。**记录实际请求与响应的头、实体摘要及必要内容，并关联语义指纹、物理前缀分歧和亲和事实。完整诊断通过受控转储保留超限实体，证据不完整时必须明示；凭证和敏感标识脱敏，日志默认关闭并按策略限域启用。

传输层报文证据用于解释实际发送与返回情况，不等同于最终网络字节已经实现原子封存。命中与费用观测不得反向改写冻结计划，缺少计价或资源费用依据时不得宣称完整成本收益。

## 11.9 当前实现状态、未闭环能力与开放条件

**能力开放必须同时具备具体供应的认证证据、协议与运行时实现，以及获批策略。**验证覆盖上下文完整性、门槛与未知资格、原生投影、用量、作用域隔离，以及资源创建未知、失租和推理发送后的恢复行为。尚未具备的维护能力应限制开放范围，不能以表结构或接口存在替代实施条件。

新协议语义先进入公共平台能力，再由配置开放；可复用现有合同的模型才通过登记与认证接入。既有供应的缓存键、历史续接和推理语义继续遵守原兼容合同，统一治理只增加必要的意图控制与可审计事实。

**能力开放与实现边界**

统一缓存执行路径包含条件筛选、统一计划、运行时评估、集中仲裁、绑定封存及用量审计。能力开放仍须分别证明**设计成立、执行链接通、具体供应认证通过**，不能由公共模块存在推导所有渠道均可执行，也不能由规划成功推导真实命中或经济收益。

- 显式资源采用确定的有效期、请求前按需验证及已知远端句柄的精确只读 GET 对账，并保持资源代次、引用保护（pin）及双围栏（Dual Fencing）恢复边界。

Grok 保留受控兼容回退路径。路径选择在受理边界确定并随请求冻结，继续遵守原有缓存键、历史续接、推理内容和存储语义；除明确缓存意图所需的控制差异及只读审计外，不得引入未授权的原生请求变化。

**重点架构提醒｜完成度 80% 以下尚未实现或未完全闭环的机制清单（编译器与生产门禁须继续限制开放）**：

1.  **受控隐式前缀心跳保活（Implicit Prefix Heartbeat Keep-alive，当前完成度 0%，尚未实现）**：属于低优先级预留设计，绝不得作为已开放能力或验收通过项，SnapshotCompiler 在编译期对任何试图开启该特性的配置继续执行 **Fail-Closed 阻断**。未来实现须经过真实流量的暖缓存判定、预期复用收益及次数预算门控，使用相同亲和作用域和稳定前缀，不得伪造业务问答、写入业务 Request / Job、推进历史或占用业务执行槽位；该机制与 Worker 的任务租约心跳属于完全不同的职责。

补充约束：未来实现还须限制生成开销；出现未命中、漂移或新业务请求时停止保活，不通过心跳重新冷启动缓存。

2.  **显式缓存与 Provider 文件的独立后台续期、自动清理、孤儿资源对账（Orphan Reconciliation）与配额收敛守护任务（当前完成度 \< 80%，未完全闭环）**：虽然数据库表结构已具备 cleanup_policy / last_used_at 且适配器提供了删除接口，但目前仅依赖请求前按需验证与 TTL 自然过期，尚未建立独立的后台守护进程闭环执行无引用资源回收与远端孤儿资源巡检对账。

3.  **Provider Files API 跨进程崩溃断点续传状态机与端到端零内存聚合流式处理（当前完成度 \< 80%，未完全闭环）**：gemini_files.py 虽已完成去渠道化重构并在二阶段上传流强制挂载 TransportGuard，但 upload_url 中间态实时落盘续传及 MaterialIngress / ObjectStorage 全链路零内存聚合流式传输仍需后续迭代闭环。

生产开放须独立验证具体供应、数据库结构及受控 RPC、API 与 Worker 配置兼容、故障恢复、长期有效期和费用对账。HTTP 实体摘要可作为诊断证据，不能替代最终字节级原子封存或完整费用账本；启动成功、健康检查和本地测试均不能单独证明生产闭环。

缓存生产开放与验收门槛必须同时对照第 15 章的专项验收清单。单元测试、规划结构或候选能力均不等于某一 Model Supply 已完成真实 Provider 的生产认证。

# 12. 安全隔离、Error 与结果边界

## 12.1 Relay API 认证

v1 与 v2 API 都使用 Bearer Token，require_relay_auth 用 secrets.compare_digest 与服务端 relay_api_token 比较。v2 router 统一挂载该依赖。

## 12.2 Owner 与资源查询隔离

Session / Request / result / raw error 等读取接口要求 X-Tenant-Id + X-Conversation-Hash，并把 owner 条件加入查询。Material 查询同样按 owner 过滤。relay_objects / relay_materials / relay_requests / binding/attempt 表启用 RLS，并撤销 anon/authenticated 访问，服务端通过 service_role 操作。

缓存资源额外受到供应、账号、区域、内容身份及请求引用保护等约束；Owner、资源回收和零保留边界见第 11.8 节。

## 12.3 成功结果边界与大对象

Provider 成功响应首先完整写 raw-response.json 与 response-output.json，并把 provisional object id 持久化，再执行 structured output parser/schema gate。即使 schema 校验失败，原始 2xx entity 仍可定位，不会被 Relay 合成错误覆盖。

成功通道继续返回 compact_result；超过 soft limit 的完整 visible text 单独保存 object，compact 中返回截断 preview 和 full_text_object_id。hard limit 只约束 compact 表示，不会通过字符截断把结构化原始响应伪造成“成功 JSON”。

## 12.4 Error：统一信封，但保留原始内容

v2 RawErrorRecorder 不再把 Provider 4xx/5xx 归一化成 UPSTREAM_BAD_REQUEST/UPSTREAM_SERVER_ERROR。ProviderHTTPError / SupabaseError 的原始 entity bytes 计算 body_size + SHA-256，存 raw-error.bin，同时提供 lossless body_base64；文本型 content-type 在严格解码成功时额外提供 body_text。归档自身失败会记录 archive_error，但不会覆盖原错误。

GET /v2/sessions/{session_id}/requests/{request_id}/error/raw 通过 owner 校验后直接返回归档原始 bytes，并附带 source、SHA、upstream HTTP status/request id 等 header。非 HTTP 异常则保存 Relay 自己实际产生的 exception type/message/cause，且明确 source。

## 12.5 Session 并发与原子提交

Session 通过 active_request_id 实施单活动 Request；accept_relay_request 在锁住 Session 后先检查幂等，再检查 SESSION_BUSY。Request 完成时 complete_relay_request 在一个数据库事务里校验 active_request_id、expected_history_version、可选 lease owner/epoch，并一起更新 Session history/version、Request succeeded 与关联 Job succeeded。

| **实现备注｜URL 安全仍有缺口：**readable_url 当前会先用 socket.getaddrinfo 校验解析结果是否为公网 IP，并禁止自动 redirect；但 httpx 真正连接时仍会再次按 hostname 解析，尚未把“已验证 IP”绑定到实际连接或检查 peer IP，因此不能把现实现描述成完全关闭 DNS rebinding。另一个细节是非 2xx source response 使用 response.aread()，未套用 material max_bytes，错误实体可能绕过输入大小上限。 |
|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

| **实现备注｜legacy v1 Error：**新 v2 Error 路径已经移除统一 UPSTREAM\_\* 分类，但 app/compatibility/legacy_worker.py 的部分 timeout/generic exception 仍使用 UPSTREAM_TIMEOUT / WORKER_ERROR，并有 message 截断。因此“兼容不继续保留旧错误归一化”在 v1 compatibility 层尚未完全收口。 |
|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

| **源码依据**：app/security.py；app/core/raw_error.py；app/api_v2/router.py；**app/execution_plane/result_committer.py**；**app/runtime/controlled_invoker.py**；sql/003\_\*.sql；sql/010_execution_plane_commit_recovery.sql；sql/011_execution_authority_closure.sql；app/materials/safe_fetch.py；app/compatibility/legacy_worker.py |
|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# 13. 故障语义、可靠性边界与未完善项

| **故障/边界**                              | **当前行为**                                                                                                                                                                                                                                                                                                                                                                                   | **设计含义**                                                                                                                                                                         |
|--------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| sync 等待超时                              | Request → indeterminate；不自动创建 Job；Session 默认不释放                                                                                                                                                                                                                                                                                                                                    | 未知上游执行不伪装成未执行，避免同步超时后二次调用                                                                                                                                   |
| async Worker 在发送前崩溃                  | not_sent + Lease 过期可安全重新 claim                                                                                                                                                                                                                                                                                                                                                          | 仍保留队列故障恢复能力                                                                                                                                                               |
| async Worker 在 Provider 已发送后失租      | dispatch_started → indeterminate；Job 不再 claim                                                                                                                                                                                                                                                                                                                                               | 不以 Lease 过期作为“上游没执行”的证据                                                                                                                                                |
| Provider 结果已保存、Session commit 前崩溃 | result_stored 可重领；只 complete_request                                                                                                                                                                                                                                                                                                                                                      | 已完成推理不重复计费                                                                                                                                                                 |
| 旧 Worker 恢复后返回                       | lease_epoch/fencing 校验阻止旧执行者提交                                                                                                                                                                                                                                                                                                                                                       | 防止陈旧结果覆盖新执行状态                                                                                                                                                           |
| Session 并发                               | 单 active_request_id；同幂等键先返回原 Request                                                                                                                                                                                                                                                                                                                                                 | 轻/重请求不能并发覆盖同一历史                                                                                                                                                        |
| Provider route/Adapter 错误                | 调用前 Fail-Closed；错误码区分 route/model/config/adapter/file adapter                                                                                                                                                                                                                                                                                                                         | 基础设施问题不再伪装成模型“正常回答”                                                                                                                                                 |
| Material binding 与 Session route 不一致   | 有 fallback 可重绑；无 fallback → ROUTE_BINDING_MISMATCH / reupload_required                                                                                                                                                                                                                                                                                                                   | 材料与 inference 必须共享 connection/account scope                                                                                                                                   |
| Provider binding 过期                      | External URL 可重签；native binding 有 fallback/source 时可刷新，否则 reupload_required                                                                                                                                                                                                                                                                                                        | Provider file 不是长期权威                                                                                                                                                           |
| Gemini External URL projection 不一致      | 若 Provider Binding 指向的 bridge object MIME、文件表示或其他 Provider-facing 属性与当前确定性 projection contract 不一致，Relay 在**新 Request 的 binding snapshot 冻结之前**优先使用 retained fallback bytes 重建 Provider Projection，并形成新的 binding generation；Canonical Material、material_id、源 MIME 与内容 hash 不变。若缺少可恢复字节，则进入 reupload_required 并 Fail-Closed。 | **Projection 修复属于 Provider Binding 恢复，不属于业务材料重写；已经冻结的 Request 不因 projection 规则或 Binding 状态变化而静默改写 snapshot，Resume 仍恢复原 Request 执行事实。** |
| 调用方重复 Request                         | 同 Session+key+hash 返回原 Request；同 key 不同 hash 409                                                                                                                                                                                                                                                                                                                                       | 逻辑调用强幂等                                                                                                                                                                       |

## 13.1 当前 Queue 与部署扩展边界

- 源码没有优先级字段与优先级 claim 排序；当前按 created_at ASC。

- 源码没有 Dead Letter Queue / Topic / Fan-out；failed / indeterminate 直接成为 Request/Job 终态或待业务处理状态。

- Worker 在 active 状态下仍按当前执行模型处理已 claim Job，横向并发仍主要依赖多个 Worker Replica。按需 wake、drain 与 quiescent 只改变 Worker 的部署生命周期，不改变 Job 领取、Lease、Heartbeat、Fencing 或 provider_dispatch_state 的恢复规则。

- Railway/SAE 仍以 execution_pool 定义 Job 的领取范围和执行边界，Session 入口/执行范围在创建时固定；该机制仍不等同于自动跨云容灾。具备 Serverless 能力的部署可以在这一执行边界之上采用 request-driven wakeup，非 Serverless 部署则可以继续保持 always-on polling。

- **API Online + Worker Sleeping 不是故障状态，而可以是“API 可接受请求、当前没有 Worker 工作需求”的正常空闲状态。** 同样，API 已经没有新的请求而 Worker 仍保持 Online，也可能表示 Worker 正在执行、drain，或等待旧 Lease 到期并完成恢复。API 与 Worker 的平台状态卡片不要求镜像同步；判断正确性的依据应是“有工作时能可靠唤醒、工作未完成时绝不提前休眠、工作全部完成后能够最终静默”。

- RouteCatalog 当前是部署内确定性目录；尚未实现数据库动态 route、健康评分或 active Session 的实时重路由。

## 13.2 当前代码未完善 / 未完全落实设计的地方

| **优先级** | **未完善点**                              | **代码事实 / 风险**                                                                                                               | **建议后续方向**                                                                                               |
|------------|-------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------|
| 高         | Provider File upload crash recovery       | attempt 表存在，但 Gemini upload_url/finalize 中间事实未实时持久化；崩溃后 Material 可长期停在 binding，幂等重放只返回旧 Material | 把 prepare 拆成持久化 phase 状态机；保存 upload_url/provider file identity；对 binding 状态做 reconcile/resume |
| 高         | readable URL DNS rebinding / error size   | 预解析公网 IP 后 httpx 再次 DNS；实际 peer 未验证；非 2xx body 直接 aread 无 max_bytes                                            | 固定已验证地址或验证 peer IP；错误 body 同样流式限长                                                           |
| 中         | 端到端流式材料处理                        | multipart/URL/API、Supabase backend、Provider File Adapter 均把整文件聚合成 bytes                                                 | 真正使用 stream/spool，hash/count 与上传并行；控制峰值内存                                                     |
| 中         | Material Idempotency 内容身份             | 同 tenant + key + 同 route 直接返回旧 Material；没有 material_request_hash 校验 filename/content/source identity                  | 增加稳定 material request identity；同 key 不同输入返回 409                                                    |
| 中         | purpose-aware RouteCatalog                | RouteIntent 有 purpose，但 catalog match 不使用 purpose                                                                           | 若未来按用途分 route，给 RouteEntry 增加 purpose/capability 条件；否则收窄文档合同                             |
| 中         | API/Worker RouteCatalog 一致性强校验      | 各进程只独立记录 revision/hash，无跨进程一致性锁或数据库登记                                                                      | 启动/ready 时登记并对比 catalog hash，Worker 拒绝未知 revision                                                 |
| 低/兼容    | v1 Error 语义残留                         | legacy_worker 仍有 UPSTREAM_TIMEOUT / WORKER_ERROR 与截断                                                                         | 在兼容层也改为 raw error delivery 或明确冻结为 legacy-only                                                     |
| 低/运维    | Provider file orphan/配额清理             | schema 有 cleanup_policy/last_used_at，Adapter 有 delete，但未见独立清理任务                                                      | 增加无引用 + retention 窗口清理与 orphan reconciliation                                                        |
| 低/迁移    | 公开 schema 仍接受 legacy connection 字段 | 路由权已收回，但字段未 forbid；默认 warn                                                                                          | Dify 瘦身完成后切 strict 并删除旧字段                                                                          |

| **重要说明：**上表把“明确属于未来能力”与“设计已提出但代码还没闭环”分开看待。动态健康路由、Kafka、R2/OSS 实际后端、atomic batch ingress 等仍是未来能力；Provider File crash recovery、DNS rebinding、legacy Error 收口等则属于现有设计目标尚未完全落实。 |
|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

缓存专项的实现成熟度与完成度 80% 以下尚未实现或未完全闭环的机制清单已集中列于第 11.9 节；本节仅保留通用 Queue、路由、材料及兼容路径的实现缺口。

# 14. Dify / 调用方集成建议

## 14.1 新集成调用路径

| **场景**         | **推荐 API**                                                  | **说明**                                                                                                                                   |
|------------------|---------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------|
| 材料准备         | POST /v2/materials                                            | 调用方传 provider/model/purpose，不传控制性 connection；保存 material_id                                                                   |
| 创建会话         | POST /v2/sessions                                             | 稳定 Idempotency-Key；Relay 解析并冻结 route；返回 session_id                                                                              |
| 轻请求           | POST /v2/sessions/{session_id}/requests，execution.mode=sync  | 仍生成 request_id；不建 Job；失败/结果可重读                                                                                               |
| 重请求           | 同一 Request API，execution.mode=async                        | 受理时生成 request_id + job_id；后续优先查 Request                                                                                         |
| 恢复             | GET /v2/sessions/{sid}/requests/{rid} → /result 或 /error/raw | 已有 request_id 时没有再次 execute 分支                                                                                                    |
| 取消             | POST /v2/sessions/{sid}/requests/{rid}/cancel                 | 停止后续 Session 提交；不能在 Provider 无取消 API 时宣称上游执行/计费一定停止                                                              |
| Dify V2 门面接入 | POST /v1/dify/relay（显式声明 execution_contract="v2"）       | 通过稳定 session_key（或已知 session_id）+ 请求级 Idempotency-Key 直接复用 v2 Session/Request 全链路能力，保留字段显式存在性与可信入口审计 |
| 旧链 V1 兼容     | /v1/dify/relay（未指定 execution_contract）或 /v1/jobs        | 仅用于既有 V1 job_id/Checkpoint 兼容；严禁将历史 V1 job_id 混入 V2 request_id 状态或结果查询                                               |

## 14.2 双层恢复的最小状态

- checkpoint_version：便于协议升级与兼容。

- pipeline / stage：业务执行位置，例如 FINAL_DRAFT_PENDING；仍只属于调用方。

- session_id：上下文、材料基座和 frozen route 的边界。

- request_id：正常 Resume 锚点；sync/async 都存在。

- job_id：仅 async Request 存在，用于队列/Worker/运维诊断和旧接口兼容。

- idempotency_key + request_hash / schema identity：覆盖受理响应未写回的缝隙，并防止同键不同请求。

- tenant_id + conversation_hash：恢复时必须与 owner 校验一致。

- provider/model/options/execution_site + upstream_artifact_ids / resume_context：锁定执行身份，只保存后续继续所需的小型引用。

## 14.3 清理时机：仍然只在业务状态持久化后

不要在 Request 刚返回 succeeded 时立即清除业务 Checkpoint。更稳妥的时机仍是：compact result 已被下游 Parser/Validator 接收，并且业务 Artifact / state_patch 已经持久化。这样即使模型成功后调用方 Parser/Assigner 崩溃，也能再次读取 request_id 恢复；如果中断发生得更早、request_id 尚未落盘，则由同一 Idempotency-Key 兜底。

## 14.4 路由字段与 Dify 职责收口

Dify / 业务 Workflow 只表达业务级 deployment、provider/model、materials、**七态标准 think_level（AUTO / OFF / LOW / MEDIUM / HIGH / XHIGH / MAX，缺省统一为 AUTO）、思考约束策略 think_level_policy（默认普通偏好 prefer，硬性要求阶段传 require）**、canonical options、requested_cache_mode、structured_output、execution 以及必要的业务元数据，不维护 ModelSupply、connection_id、账号、ProtocolProfile、ProviderAdapter、渠道优先级、配额预算、Provider File 状态机、缓存资源生命周期、**各模型具体支持哪些思考档位/Token 预算映射**或 Native Wire。思考控制与缓存控制对调用方均保持纯粹的意图级抽象：调用方无需针对不同模型编写 thinkingConfig、reasoning_effort、budget_tokens 分支逻辑，也无需感知缓存 TTL、读写策略、缓存 key、资源句柄、cache_control、cachedContent 或断点位置。若业务需要限定渠道，应通过受治理的 deployment / routing policy 表达候选范围，而不是直接指定内部 connection 或绕过 Route Policy。

调用方仅负责表达缓存意图；具体供应资格、effective mechanism、原生字段投影及不可跨渠道恢复规则，统一见第 11.2、11.4、11.6 节。

| **迁移备注：**Relay 已能把旧 connection hint 降权为 warn/strict assertion，但调用方是否已经完全删除旧字段不在本源码包内。生产切 strict 前应先通过日志确认 legacy_connection_hint_mismatch 已归零。 |
|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# 15. 运维与验收建议

以下验收项一部分已由当前 68 项测试覆盖，一部分是基于代码边界必须补做的部署/故障注入测试。与旧版相比，验收重点从“Job 能否恢复”扩展为“Request 身份、RouteBinding、Material Binding 与 Provider dispatch 阶段是否保持稳定”。

6.  Session 幂等与路由冻结：相同 Session key+intent 返回原 Session；catalog revision 更新后旧 Session connection 不变。

7.  sync 无 Job：创建 sync Request，确认 relay_requests 有记录、relay_jobs 无新行；结果/错误可通过 request_id 重读。

8.  async 同事务：创建 async Request，确认 Request 与唯一 Job 同事务出现；状态 queued → leased/running → succeeded。

9.  Session 单活动 Request：运行 async 重请求时提交另一个不同幂等请求应 SESSION_BUSY；同键重试先返回原 Request。

10. Lease/Fencing：发送前 kill Worker 可安全重领；dispatch_started 后 kill Worker 应转 indeterminate 而不是再次 Provider 调用；result_stored 后 kill 应只恢复 commit。

11. Route Fail-Closed：provider=gemini + legacy aihubmix_default 在 warn 模式仍实际走 Gemini Native；strict 模式 409；route 缺凭据与 route 不存在返回不同错误。

12. Gemini 单材料/多材料：实际总字节 ≤99 MiB 走 External URL；多个小文件合计 \>99 MiB 在 Request freeze 前 promotion 到 Files API；retry/resume 复用 frozen binding snapshot。

13. Signed URL normalization：Supabase /object/sign/... 与 storage/v1/object/... 等相对形式都生成可访问的 /storage/v1/... 绝对 URL。

14. Gemini External URL Projection 一致性：分别使用 JSON 文本 Artifact、PDF 及多材料组合验证 Canonical Material 与 Provider-facing representation 的隔离。JSON Material 在材料事实层必须继续保持 application/json、原始文件名、内容 hash 与 material_id，而 Gemini External URL bridge 应投影为 text/plain 与一致的文本文件表示，并确认 Signed URL 实际 HTTP Content-Type 与最终 fileData.mimeType 一致；PDF 等已兼容材料应继续保持 application/pdf 与原文件表示，不产生无意义转换。对已有但不符合 projection contract 的 External URL bridge，应验证其在**新 Request freeze 前**能够从 retained bytes 重建并产生新的 binding generation；已冻结 Request 的 Resume 则必须继续复用原 binding snapshot。材料总量跨越阈值时仍只改变 External URL / Files API transport，不改变 Canonical Material。**验收标准不是“URL 能打开”，而是“Canonical Material 保真、Provider Projection 一致、Transport 选择稳定、Request Snapshot 不被恢复过程静默改写”。**

15. Kimi 语义：材料侧验证文本 file-extract → extraction artifact，图片/视频使用 ms://，不把 text file_id 直接当模型正文；思考侧验证 **Kimi K2.7 Code** 的 **fixed_on** 合同：AUTO 可按固定开启语义执行；LOW / MEDIUM / HIGH / XHIGH / MAX 在 prefer 下允许收敛为同一 effective fixed_on + enabled，但对应深度映射的 **satisfies_require 必须为 False**，因此 require 下的具体深度硬要求必须拒绝；**OFF** 必须按 fixed_on_cannot_disable 拒绝。Adapter 固定发送 thinking.type=enabled，且不发送 reasoning_effort。在输入、材料、options、schema、execution 及 think_level_policy 等其他执行事实不变时，仅改变调用方开启类思考深度标签不应形成新的 K2.7 执行身份；但 prefer 与 require 不同必须形成不同 **request_hash**。Kimi K3 的可调档位则应按 capability 映射为 reasoning_effort，auto 使用模型默认行为。

16. Raw Error：Provider/Storage 的 JSON、text、HTML、binary/压缩 entity 可按 hash/base64/原始 bytes 读取；归档失败不覆盖原错误。

17. Structured Output：任意 canonical JSON Schema 经过 provider projection 后仍用原 schema post-validation；不兼容时 Fail-Closed，不退化成无约束 JSON。

18. Owner 隔离：错误 tenant/conversation 查询 Session/Request/result/raw error/material 应不可读。

19. Worker 按需唤醒：先使 Worker 进入 quiescent 并最终由平台进入 Sleeping，在 API 仍可正常接受请求的情况下发起新的工作请求；验证 API→Worker 私网 wake 能使 Worker 恢复 active，并继续完成 queued → leased/running → terminal 的正常消费链路。**API Online + Worker Sleeping 本身不应作为失败判据；真正的验收点是新工作到达后 Worker 能否被可靠唤醒并继续消费。**

20. Worker Drain / Sleep Gate：构造正在执行的 Job、尚可领取的 queued Job，以及旧 Worker 实例遗留且 Lease 尚未过期的 leased/running Job；停止新的 API 工作活动后，验证 Worker 在任一未完成条件存在时都不会进入 quiescent。旧 Lease 到期后必须先执行既有 not_sent / dispatch_started / result_stored 恢复规则，直到当前 execution_pool 不再存在可执行或有效未完成任务，才允许停止 DB/provider polling。**Worker 的休眠前提是“所有相关工作已经完成或进入确定终态”，而不是“这一轮没有 claim 到 Job”。**

21. 生命周期控制 Fail-Safe：模拟 Worker 控制服务异常、私网控制链路不可用或 wake/sleep 控制状态不可确认，验证 Worker 保持或回退为 always-on polling，不因控制面异常进入无法可靠唤醒的休眠状态；控制链路恢复后，再验证 request-driven wake 与 drain-to-quiescent 能重新正常工作。

22. 零代码模型接入：选择一个完全落在已注册 Protocol Profile 与运行时能力覆盖范围内的新模型，仅新增 Model Catalog / Model Supply / Capability Contract / Route Policy 配置并完成验证发布；业务 Workflow、Relay Core 与协议实现不增加模型专属分支。

23. 同模型多渠道优先级：为同一模型配置至少两个合格供应，并分别设置不同 priority；验证“先资格、后排序”，高优先级但缺少硬能力、材料条件、配额、预算或安全资格的供应不会被选中；调整 priority 只影响新 Session，已有 Session 继续使用 frozen Model Supply。

24. **思考能力治理与跨层语义闭环专项验收**： （1）**七态意图、来源精确取证与 Dify V2 门面透传**：覆盖 AUTO、OFF（含 none/disabled/false 别名）、LOW、MEDIUM、HIGH、XHIGH、MAX 七态输入，以及未传参（omitted）、显式空值（explicit_empty）、显式传值（caller_explicit）三类来源；验证省略 think_level 时默认采用 AUTO 且保持省略投影（wire_action=omit），显式 AUTO 按合同 omit_on_explicit_auto 区分省略或发送原生自适应结构，非法字符串与 Dify V2 未知请求字段均返回 422；验证服务端可信入口审计不可由客户端 Header 伪造且不改变 request_hash。 （2）**Excel 单一信源编译与准入自动推导**：验证删除《模型清单.思考档位》及《能力合同》重复白名单后，编译器仅从《思考映射清单》《思考合同配置》《供应思考绑定》编译七态规则、支持按模型匹配拆分供应绑定，并自动生成供应级 hard_satisfied_intents 准入摘要；构造缺失档位、重复档位、预算/档位单调性倒挂、OFF 映射为开启、fixed_on 允许 OFF 或宣称满足深度硬约束、未注册投影策略等异常配置，验证编译期直接阻断并精确输出工作表名与行号。 （3）**prefer / require 双轨策略与六种模式映射**：在 none、toggle、fixed_on、discrete、budget、adaptive 六种模式下，验证普通偏好（prefer）不支持时按映射表平滑适配（如仅支持自适应的模型接收 LOW 映射为 adaptive/omit，离散模型接收 XHIGH/MAX 钳制为 high），而硬性要求（require）在 satisfies_require=False 时严格 Fail-Closed；验证固定开启模型拒绝 OFF 及深度硬要求，预算型模型在 satisfies_require=True 时通过 require 校验。 （4）**思考激活态采样互斥与跨轮次缓存指纹不变性**：验证思考激活时违反禁用温度、禁用 top_p、固定温度或预算小于最大输出上限的请求在执行面被拦截，且未传采样参数时不自动补写被禁用的默认值；验证同一 Session 跨轮次在 LOW、HIGH、MAX 之间切换思考档位时，ContextPlan 内容指纹、历史只读前缀指纹（cache_fingerprint.py）与 Gemini cachedContents 显式缓存完全复用且创建边界拒绝混入单轮思考配置。 （5）**有效语义幂等收敛与存量冻结兼容**：验证不同请求标签收敛至同一有效思考语义且 policy 相同时产出相同 request_hash；同时验证相同有效语义但 policy 不同必须产出不同 request_hash，observed_thinking 永不参与 request_hash；验证存量旧格式冻结 Session 通过内存兼容转换正常受理新请求而不改写快照，历史已受理 Request 重放保持原身份哈希与原投影。 （6）**observed_thinking 双模取证、后置核验与 result_stored 恢复门禁**：验证五类协议原生响应体与受控网关响应头正确提取 ObservedThinkingEvidence（未上报计数保持 None 不当作 0）并归档至 execution-fact.json；模拟上游网关剥离/降级思考参数或在关闭思考状态下返回显式思考内容块/正数思考 Token，验证 ResultCommitter 在正常提交与 result_stored 恢复路径均判定违约失败并保留原始 2xx 响应引用，且缺失 observed_thinking 归档时拒绝走历史直接提交分支。

25. Structured Output 保证：分别验证 native constrained 与 post-validated 模式；允许的 deterministic schema projection 仍以 canonical schema 最终校验；模拟渠道静默移除 schema 约束时，要求 native constrained 的调用必须被识别为合同不满足。

26. 聚合渠道 actual model / fallback：模拟上游模型映射、跨模型 fallback 与免费转付费场景；验证未获 Route Policy 和预算授权的变化不能被当作正常成功，原始响应与 actual_model / routing metadata 可追溯。

27. Config Snapshot 治理：验证草稿不能进入生产执行；审核后的快照可以灰度、原子发布和回滚；API / Worker 对未知或不兼容 config_snapshot_id Fail-Closed；控制面短暂不可用时可以继续使用最后一个已验证快照，且不会重新解释已有 Request。

28. 资源与预算：在共享账号下验证并发、请求频率、token 配额与成本预算对所有执行节点一致生效；免费供应达到限制后，只能选择已批准且仍满足能力合同的候选，或明确失败，不发生未经授权的付费兜底。

29. 多渠道 Material：同一 material_id 在不同 channel / account scope 建立独立 Provider Binding；验证 Provider file ID 不跨账号复用，Request freeze 前可以从 retained bytes 确定性 rebind，freeze 后 Resume 始终复用原 material_binding_snapshot。

30. 路由与恢复边界：在 not_sent、dispatch_started、result_stored 三个阶段分别制造渠道故障；验证优先级和 fallback 不穿透既有 Request recovery，dispatch_started 未知执行不会因为存在备用渠道而再次推理。

**缓存专项验收：**

（1）意图、默认与幂等：分别验证 off、auto、on 及字段省略行为。默认仅作用于省略字段，既有会话合同保持冻结；明确低于门槛时继续完整上下文的普通推理，其他合同不满足按规定拒绝。同一幂等键改变缓存意图应检测冲突，幂等复用须满足解析语义与恢复边界等价。

（2）语义等价与历史冻结：验证结构化键序等合法差异能够规范化，业务正文、角色、轮次、Part 顺序、材料身份及有效约束保持不变。后续阶段变化不得重新生成已提交历史正文，无法证明等价的附件拆分必须拒绝。

（3）统一计划与集中仲裁：验证先筛选条件，再形成唯一完整计划；相同冻结事实能够重放相同逻辑结果。运行时评估不得提前创建缓存或发送模型推理，只有选定分支进入物化；待决条件、失败分支和裁决依据可追溯，同步与异步执行保持语义等价。

（4）组合关系与供应认证：分别验证三类机制的实际原生投影及用量映射。Gemini 执行显隐互斥；Claude 仅在双重授权、适用性及覆盖包含关系均成立时形成组合。官方、直连与聚合供应分别认证，不继承彼此的门槛、端点、费用、错误语义或资源可见域。

（5）副作用与恢复：覆盖并发创建、创建结果未知、失租、资源到期、绑定失效及结果待提交等场景，验证资源操作与请求执行的独立 fencing、引用保护和恢复边界。**首次模型发送后自动换机制、换渠道或再次推理的次数必须为零。**

（6）首轮与滚动成本：验证合格首轮能够准备选定缓存路径，后续优先复用合法旧快照；重建符合前缀一致性、增长迟滞和成本门控。未知成本不按零处理，试用授权与预计收益单独标记，不以首轮准备成功承诺后续必然命中。

（7）材料预算与 DOCX 完整性：验证默认 60 MiB 预算下的分级密度选择、确定性和完整透传；优先级不改变上下文顺序。DOCX 表格内容控件、修订包装及未认证结构不得静默遗漏；桥接不合格时，仅可采用已授权原生表示，否则在模型发送前拒绝。不合格派生产物不得被错误复用。

（8）隔离、亲和与漂移：不同 Session 即使内容相同，也不得共享 Relay 管理的缓存资源。验证亲和标识在合法作用域内一致使用，缺失用量不当作零，单次零命中不直接认定漂移；淘汰受发送前执行权保护，不能改变冻结供应或已发送请求。

（9）报文、用量与费用证据：验证诊断来自实际 HTTP 传输层，凭证脱敏、超限实体及证据完整性可追踪。Claude 原生缓存读取、写入和未缓存输入分别保留，完整输入分母正确；真实零值与未知值区分，别名不重复计数，实际费用归属于真正执行的分支。

（10）兼容回退：验证 Grok 路径选择随请求冻结，缓存键、历史续接、推理内容、存储行为及原生请求遵守兼容合同，已受理请求恢复不重新选择路径。

（11）开放门禁与预留能力：真实供应、数据库结构及 RPC、跨进程配置兼容、长期有效期、故障注入和费用对账分别留存证据。**隐式前缀心跳保活完成度为 0%，后台维护未闭环部分单独列为预留，不进入已通过清单。**

- **Google 官方 Gemini 直连渠道全链路与合同验收**：验证通过声明式配置（google_official.declaration.json / 配置工作簿）登记并发布官方 Supply 后，请求走 generativelanguage.googleapis.com 与 x-goog-api-key 鉴权、无网关亲和头；验证小体积与大体积材料均按官方合同走 Files API 生成官方 fileUri（不生成外部 Supabase Signed URL），且文本型 JSON 构件仍保持 text/plain 确定性投影；验证显式缓存启用 file_uri_allowed 模式后直接将官方 fileUri 投影进 cachedContents 且不受 60 MiB 内联字节预算裁剪；验证 native_constrained 结构化输出通过 first_party_native_payload 原生响应体取证顺利完成 ResultCommitter 合同核验。

- **同协议任意命名双独立连接隔离盲测**：在不修改任何 Python 代码的前提下，通过配置注册两条任意命名（如 conn_gemini_alpha 与 conn_gemini_beta）、同属 Gemini 协议但指向不同 account_scope_id 的独立连接与 Supply；验证公共工厂自动装配各自独立的推理、文件与缓存客户端实例，且 alpha 与 beta 会话之间的 provider_material_bindings 文件句柄与 cachedContents 缓存资源严格物理隔离、绝不串用。

- **已有协议覆盖下第三方渠道零代码接入验证**：任选一个由既有协议适配器（如 ChatCompletions、Responses 或 ClaudeMessages）覆盖的新供应渠道，仅修改 Excel 工作簿登记 ConnectionSpec、ProtocolProfile、CapabilityContract 与 ModelSupply，配置密钥并重新部署，验证系统自动完成配置读取、校验、编译、保存与加载，且 API 与 Worker 消费同一配置版本的运行快照；全程无需手工维护发布 JSON 或激活指针，不改动 app/ 下任何业务与协议代码即可完成装配、选路、推理与审计闭环。

- **多渠道环境下围栏拦截与不可逆门槛防重发红线验证**：① 在官方渠道推理、SDK 文件二阶段分片上传及显式缓存创建发包前注入失租，验证统一工厂注入的 TransportGuard 100% 拦截请求，实际发往上游的 HTTP 报文数为零；② 验证仅在单一渠道持有 provider_bound 绑定且无 fallback 原始字节的材料，在创建新 Session 时由 material_reachability 前置排除无法重建绑定的其他渠道；③ 在高优先级官方渠道跨过 AtomicDispatchSeal 进入 dispatch_started 后注入网络超时或杀死 Worker，验证权威恢复严格将其置为 indeterminate 并保留资源 pin，**绝不因为存在备用的低优先级 AIHubMix 渠道而穿透恢复边界静默换渠道重发，也绝不去缓存重发（重发次数严格为零）**。

| 验证记录应标明源码、配置、计划解释规则、数据库合同与测试环境的对应关系。本地检查、受控集成测试和真实供应认证分别记录，生产开放依据具体供应的完整证据作出判断。 |
|----------------------------------------------------------------------------------------------------------------------------------------------------------------|

| **建议新增专项故障注入：**重点补三类当前测试未闭环场景：① Gemini upload session 创建后、finalize 前/后进程崩溃与恢复；② DNS rebinding/peer IP 变化及巨大非 2xx readable URL body；③ Material 同 Idempotency-Key 但输入内容变化的冲突检测。 |
|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# 附录 A. API / Request / Job / Material 状态速查

| **接口**                                               | **用途**                    | **关键输入**                                                                                                        | **典型输出**                                                         |
|--------------------------------------------------------|-----------------------------|---------------------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------|
| GET /health                                            | 健康与版本/route revision   | —                                                                                                                   | version、deployment_id、execution_pool、route_revision/providers     |
| POST /v2/materials                                     | 创建/复用 Material          | Idempotency-Key + owner/body + provider/model/purpose                                                               | 201 MaterialResponse；material_id/status/durability/binding          |
| GET /v2/materials/{material_id}                        | 查询材料                    | owner headers                                                                                                       | MaterialResponse                                                     |
| POST /v2/sessions                                      | 创建/复用 Session           | Idempotency-Key + provider/model/material_ids                                                                       | 201 session_id + history_version + route_revision                    |
| GET /v2/sessions/{session_id}                          | 查询 Session                | owner headers                                                                                                       | SessionResponse                                                      |
| POST /v2/sessions/{session_id}/requests                | sync/async 唯一模型调用入口 | Idempotency-Key + owner + input/materials/think_level/think_level_policy/options/execution/schema                   | RequestEnvelope；始终 request_id，async 另有 job_id                  |
| GET .../requests/{request_id}                          | 查询 Request                | owner headers                                                                                                       | status/result/error summary；查询时 reconcile                        |
| GET .../requests/{request_id}/result                   | 读取结果                    | owner headers                                                                                                       | 202 queued/leased/running；终态 200 Envelope                         |
| GET .../requests/{request_id}/error/raw                | 读取完整 raw error          | owner headers                                                                                                       | 原始 bytes + diagnostic headers                                      |
| POST .../requests/{request_id}/cancel                  | 取消 Request                | owner headers                                                                                                       | cancelled / 既有终态                                                 |
| POST /v1/dify/relay（execution_contract="v2"）         | Dify 显式 V2 传输门面       | execution_contract="v2" + session_key / session_id + Idempotency-Key + canonical Request body（保留字段显式存在性） | V2 RequestEnvelope；复用 v2 Session/Request 全链路并保留可信入口审计 |
| /v1/dify/relay（未指定 execution_contract）或 /v1/jobs | Legacy Job 兼容             | 旧 JobSubmitRequest / job_id / Checkpoint                                                                           | 旧 job_id 协议；不代表 v2 新主接口                                   |

## A.1 Request 状态

| **Request 状态** | **含义**                                                                      |
|------------------|-------------------------------------------------------------------------------|
| queued           | async Request 已受理并创建 Job，尚未执行                                      |
| leased           | 调度相关状态可投影到 Request/Job；Worker 已持有 Lease                         |
| running          | Inline/Worker 正在执行或已开始 Provider dispatch                              |
| succeeded        | 结果已通过 complete_relay_request 原子发布，provider_dispatch_state=committed |
| failed           | 确定失败；error 保存原始/实际失败信息；通常释放 Session                       |
| cancelled        | 调用方取消；不再提交 Session 历史                                             |
| indeterminate    | Provider 可能已收到/执行，但 Relay 无法证明最终结果；默认不自动重发           |

## A.2 Provider dispatch 状态

| **provider_dispatch_state** | **恢复含义**                                                                |
|-----------------------------|-----------------------------------------------------------------------------|
| not_sent                    | 尚未向 Provider 发送；Lease 过期可安全重领                                  |
| dispatch_started            | 已经进入 Provider 调用；失租/进程消失后保守转 indeterminate                 |
| result_stored               | Provider 成功实体与 provisional result/history 已持久化；可只恢复数据库提交 |
| committed                   | Session/history、Request、关联 Job 已原子发布终态                           |

## A.3 Job 状态

| **Job 状态**  | **含义**                                            |
|---------------|-----------------------------------------------------|
| queued        | async 调度记录已持久化，尚未被 Worker 领取          |
| leased        | Worker 原子领取，持有 lease_owner + lease_epoch     |
| running       | 执行中，Heartbeat 续租                              |
| succeeded     | 关联 Request 已原子提交成功                         |
| failed        | 关联 Request 确定失败                               |
| cancelled     | 调用方取消                                          |
| expired       | Job TTL 终止状态，主要保留旧/通用状态语义           |
| indeterminate | dispatch_started 后租约过期，未知 Provider 最终结果 |

## A.4 Material 状态与 durability

| **类别**   | **值**                     | **含义**                                            |
|------------|----------------------------|-----------------------------------------------------|
| status     | receiving / binding        | 接收或建立 Provider binding 中；Request 不可使用    |
| status     | ready_provider / ready     | Provider 或 Relay 路径可用                          |
| status     | fallback_stored / degraded | 已有 fallback，但目标 Provider binding 未必 ready   |
| status     | reupload_required          | binding/source/fallback 都不足以恢复，需要重新上传  |
| status     | failed / deleted           | 终态不可用                                          |
| durability | provider_bound             | 只有 Provider binding，Relay 不保证长期持有原始字节 |
| durability | relay_backed               | Relay 有 fallback bytes，可重签/重绑                |
| durability | source_recoverable         | 受控源仍可重新读取                                  |
| durability | reupload_required          | 无法自动恢复材料                                    |

# 附录 B. 源码追踪索引

| **层次 / 主题**                | **源码位置**                                                                                                                                                                                                                                                          | **关键符号 / 职责说明**                                                                                                                                                                                                                                         |
|--------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **API 接入与契约**             | app/api.py；app/api_v2/router.py；app/v2_models.py                                                                                                                                                                                                                    | /health 快照兼容性检查；**v2 纯 HTTP 接入层**；RequestEnvelope 与公共请求/响应模型                                                                                                                                                                              |
| **控制面：快照与目录**         | app/control_plane/snapshot.py；app/control_plane/catalog.py；app/control_plane/definitions.py；app/control_plane/supply_group.py                                                                                                                                      | **不可变配置快照 (ImmutableConfigSnapshot)**；RouteCatalog、受管供应组与连接/能力/协议静态声明                                                                                                                                                                  |
| **控制面：授权与离线治理**     | app/thinking_contracts.py；app/control_plane/governance/thinking_mappings.py；app/control_plane/cache_contracts.py；app/control_plane/execution_authorization.py；app/control_plane/governance/\*                                                                     | 七态思考意图与三层语义不可变契约；**Excel 思考映射单一信源离线编译、静态门禁校验与供应准入摘要自动推导**；缓存能力子合同、上游执行授权合同、SnapshotCompiler 离线编译与自动部署验证门禁                                                                         |
| **执行面：会话与选路**         | app/execution_plane/session_manager.py；app/execution_plane/route_resolver.py；app/execution_plane/supply_resolver.py                                                                                                                                                 | 消费不可变快照完成供应资格过滤、渠道优先级选路与 **Session RouteBinding 冻结**                                                                                                                                                                                  |
| **执行面：受理与逻辑计划**     | app/execution_plane/request_acceptor.py；app/execution_plane/semantic_resolver.py；app/execution_plane/thinking_resolver.py；app/execution_plane/context_planner.py；app/execution_plane/cache_intent_planner.py；app/execution_plane/request_identity.py             | 捕获字段显式来源与可信入口审计；**解析 ResolvedThinkingSemantics（映射优先、prefer/require 裁决、存量合同内存兼容转换、思考激活态与采样参数联合校验）**；构建并冻结 FrozenExecutionPlan；确保缓存内容身份与单轮思考控制正交隔离；按有效语义与策略计算幂等身份   |
| **执行面：调度与恢复提交**     | app/execution_plane/schedulers/\*；app/execution_plane/result_committer.py；app/execution_plane/result_contract.py；app/execution_plane/structured_output.py                                                                                                          | 同步/异步调度与心跳联动取消；Worker 按需唤醒与 Drain 门控；**强制核验 observed_thinking 归档完整性的 result_stored 只提交恢复**；Canonical Schema 后置校验、上游通用执行授权核验、思考执行合同后置核验（verify_thinking_execution_contract）与 Session 原子提交 |
| **运行时：引擎与围栏核验**     | app/runtime/engine.py；app/runtime/condition_verifier.py；app/runtime/execution_guard.py；app/runtime/transport_guard.py                                                                                                                                              | **RelayRuntimeEngine** 六步物理流水线编排；阶段边界与底层 HTTP 发包前的活跃 **ExecutionFence** 核验                                                                                                                                                             |
| **运行时：材料与缓存物化**     | app/runtime/material_binder.py；app/runtime/physical_context.py；app/runtime/cache_arbiter.py；app/runtime/cache_economics.py；app/runtime/cache_materializer.py；app/runtime/stateful.py；app/runtime/breakpoints.py；app/runtime/prefix.py；app/runtime/affinity.py | 落实 **Material Binding**（99 MiB 阈值选择与确定性投影）；60 MiB 缓存密度准入、物理前缀指纹、**只读评估与集中成本仲裁**；双围栏缓存物化与已知句柄 GET 对账，产出 **CacheExecutionBinding**                                                                      |
| **运行时：原子封存与受控调用** | app/runtime/dispatch_sealer.py；app/runtime/controlled_invoker.py；app/persistence/execution_artifacts.py                                                                                                                                                             | 事务性锁定资源、建立 pin、生成 **SealedDispatchBundle** 并写 **dispatch_started**；单次调用协议适配层、归档 raw/output/execution-fact 并写 **result_stored**，产出 **ActualExecutionFact**                                                                      |
| **协议适配层**                 | app/protocol_adapters/base.py；app/protocol_adapters/thinking_projection.py；app/protocol_adapters/thinking_evidence.py；app/protocol_adapters/profiles/\*；app/protocol_adapters/wire_projection/\*；app/protocol_adapters/usage_normalizer.py                       | 入口复核封存摘要与冻结材料摘要；**基于注册策略表消费冻结 EffectiveThinkingSemantics 执行五类协议确定性 Native Wire 投影**；双模提取通用响应证据与 **ObservedThinkingEvidence** 并输出 StandardizedProviderResult                                                |
| **材料入库与持久化基座**       | app/materials/ingress.py；app/materials/document_projection.py；app/materials/provider_files/\*；app/v2_repository.py；app/core/raw_error.py；sql/003\_\*.sql～sql/011\_\*.sql                                                                                        | Canonical Material 入库、DOCX 完整性桥接、Provider 原生文件上传、Raw Error 保真归档、Postgres 事务与**最终执行权围栏 RPC**                                                                                                                                      |
| **Legacy 兼容与 Dify V2 门面** | app/compatibility/dify_v2.py；app/compatibility/relay_gateway.py；app/compatibility/legacy_worker.py；app/compatibility/fusion_runtime.py                                                                                                                             | 旧 /v1 Job 接口与 Fusion 运行时兼容路径；**通过 TrustedDifyEntry 注入服务端可信入口作用域、保留 exclude_unset 显式存在性并直接桥接 v2 核心链路的 execution_contract=v2 显式传输门面**                                                                           |

# 附录 C. 答案融合、订单支付与 Relay 恢复映射

| **订单支付概念**          | **答案融合概念**                 | **Relay 恢复含义**                                                           |
|---------------------------|----------------------------------|------------------------------------------------------------------------------|
| merchant_payment_no=M1001 | idempotency_key=K400             | 同一业务意图的稳定受理身份；用于 request_id 尚未可靠回写的缝隙兜底           |
| payment_id=P1001          | request_id=R400                  | 一次外部执行事实的稳定身份；sync/async 都存在，是正常 Checkpoint Resume 锚点 |
| 支付平台内部队列任务      | job_id=J400                      | 只在 async 时存在的后台调度身份；不是业务恢复唯一主键                        |
| PAYMENT_PENDING           | FINAL_DRAFT_PENDING              | 业务状态机表明“已推进到此阶段，等待外部执行事实”                             |
| queryPayment(P1001)       | GET Request R400 → status/result | 正常恢复直接查询原外部执行，不重新创建模型调用                               |
| 同 M1001 重试支付受理     | 同 K400 重放相同 Session Request | 命中原 payment/request；同键不同请求应拒绝                                   |
| 支付处理器 fencing        | relay_jobs.lease_epoch           | 旧执行者不能在失租后覆盖新执行状态                                           |
| 支付平台未知最终状态      | Request indeterminate            | 已发送但最终结果不可证明；需要对账/业务决策，不能默认再扣/再推理             |

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<thead>
<tr class="header">
<th><p><strong>最终结论</strong>：Model Relay 以 <strong>Session + Request + Material + RouteBinding</strong> 保持稳定调用身份、材料事实、恢复与原子提交边界，并通过<strong>控制面、执行面、运行时、协议适配层</strong>四层正交解耦的架构分工，构建起配置驱动、能力驱动、协议可复用的模型中继系统：</p>
<ul>
<li><p><strong>控制面</strong>专注管理、认证和发布供应、能力、协议及策略，向下游输出只读的<strong>不可变配置快照（ImmutableConfigSnapshot）</strong>；</p></li>
<li><p><strong>执行面</strong>专注受理请求、解析有效语义、冻结逻辑计划（产出 <strong>Request、ContextPlan、CacheIntentPlan</strong>）、编排同步/异步调度与只提交恢复，并在模型返回后完成 <strong>Canonical 合同校验与结果/历史原子提交</strong>；</p></li>
<li><p><strong>运行时</strong>专注核验活跃执行权条件，落实材料表示（<strong>Material Binding</strong>）、缓存只读评估、集中成本仲裁与选定资源物化（<strong>CacheExecutionBinding</strong>），通过<strong>发送前原子封存（AtomicDispatchSeal）</strong>建立不可逆派发门槛，完成单次受控调用并落盘<strong>实际执行事实（ActualExecutionFact）</strong>；</p></li>
<li><p><strong>协议适配层</strong>专注消费已封存事实构建 <strong>Native Wire</strong>，解析原生响应与观测证据，向上游返回<strong>标准化 Provider 结果（StandardizedProviderResult）</strong>。</p></li>
</ul>
<p>缓存与材料管理均不作为协议适配层的局部技巧，而是完整融入上述四层契约链。usage、命中、实际模型与计费层级属于上游观测事实，只用于合同核验与审计，绝不反向改写已冻结的逻辑执行身份。<strong>判断一项能力是否真正进入 Model Relay 架构的标准，不是某个 Adapter 是否拼接了对应字段，而是它是否完整进入了控制面认证快照、执行面身份与逻辑计划冻结、运行时物理绑定与双围栏原子封存、协议适配层只读 Wire 投影以及执行面最终合同核验与原子提交的四层受控闭环。</strong></p></th>
</tr>
</thead>
<tbody>
</tbody>
</table>
