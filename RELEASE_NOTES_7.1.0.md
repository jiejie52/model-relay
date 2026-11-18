# Model Relay 7.1.0 架构优化缺口补全

唯一源码基线为用户上传的 `model-relay-7.0.0-source.zip`，SHA-256：

`7016067d7591325d1f9bf6486977c66af4ed7a698bbd5c08de665e060d089b78`

按已确认的修改计划补全处理方式，版本递增为 7.1.0。原始压缩包未修改，依赖清单不变；本包不新增 tests 文件。7.0.0 及更早发布说明作为历史记录保留，本说明和当前源码为本次变更依据。

## 1. 收敛为一套 v2 执行实现

- 删除 `app/cache/`、`app/providers/`、`app/routing/`、`app/execution/`、`app/core/execution_runtime.py` 及重复的内部导入转发文件；运行代码对这些退休路径的导入为零。
- 真正的 v1 `fusion_runtime.py`、`relay_gateway.py` 移至 `app/compatibility/`，旧 API / Worker 兼容入口显式引用它们；不保留另一套 v2 单体执行器。
- 删除 HTTP 路由内未使用的受理辅助逻辑，唯一实现留在执行面。`app/worker.py` 与 `app/governance/cli.py` 仅保留必要的公开启动入口；这些入口不包含第二套业务逻辑。

| 归属 | 当前职责 |
| --- | --- |
| 控制面 | 声明、合同、资源适配器登记、离线编译、发布配置及只读目录 |
| 执行面 | 会话与请求受理、有效语义、逻辑计划、调度、合同校验、历史与终态提交 |
| 运行时 | 活跃执行权、物理材料与缓存、成本观测、资源物化、封存、单次调用、原始事实落盘 |
| 协议适配层 | 消费封存输入，投影 Native Wire，解析响应与用量；不自行选路、切缓存或重发 |

## 2. 最终 ExecutionFence 与原子发送封存

新增 SQL 011 的统一最终校验同时检查 Request 状态与派发阶段、非空 owner、正数 epoch、执行主体状态、非空且严格晚于数据库 `clock_timestamp()` 的 lease。异步还核对 Job 与 Request 的关联；同步核对 Request 自身执行权。已失效的租约不能通过续租复活。

材料绑定安装、缓存绑定安装、发送封存、结果存储、提交及失败写入均接入该校验。封存先锁定全部关联资源，核对租户、对话、Session、connection、generation、有效期和就绪状态；材料绑定必须已安装，CacheBinding 必须对应当前 CachePlan。创建 pin、封存绑定、保存摘要和推进 `dispatch_started` 在同一事务中完成。资源处理后再次检查执行权，失败抛异常，使已写 pin 一并回滚。

`relay-prepared-execution/2` 摘要覆盖完整冻结快照、Session、历史、材料 ID 与绑定、所有者与 Request/Session ID、ContextPlan、CacheBinding、ExecutionFence、完整物理计划及亲和请求头。适配器入口重算物理计划内容摘要，再核对整体封存摘要；兼容字段 `_relay_cache_execution` 必须与正式绑定相同。文本材料使用冻结对象及内容摘要，不能在投影时改读最新 Material；原始字节和文档投影均检查内容身份。

封存是 **Adapter 输入的原子授权与完整性记录**，不宣称数据库事务与远端 HTTP 字节发送构成分布式原子事务。

## 3. Heartbeat 失效与恢复路径

执行面持有执行任务和心跳任务。续租返回 false、异常或超时会永久标记当前执行权失效，取消并等待执行子任务退出。运行时在物理阶段边界检查执行权，并在最底层 HTTP transport 发包前再次校验；缓存 create 同时检查独立 Operation 围栏。日志模块仅保留报文观测，发送许可由运行时传输围栏负责。SDK 文件上传和缓存网络调用也使用该边界。

| 已持久化阶段 | 执行权失效后的行为 |
| --- | --- |
| `not_sent` | 当前执行者停止；仅在重新取得新 epoch 后才可恢复；未知缓存创建不可盲目重建 |
| `dispatch_started` | 可能已发送；权威恢复置 `indeterminate`、保留 Session 占用与 pin；不换渠道、不重发推理 |
| `result_stored` | 保留结果，后续执行者只加载事实并校验、提交；不进入运行时或适配器 |
| `committed` | 保持既有终态 |

同步 deadline 与执行租约有独立余量，心跳失败也会终止同步子任务。同步提交未完成时，仅在子任务已退出且结果已持久化的条件下释放匹配的提交权，以便相同 POST / Idempotency-Key 立即恢复。GET 保持只读。异步重新领取只允许未发送或已存结果；发送已封存且失租的 Job 统一走权威恢复。

取消无法撤回已经到达 Provider 的请求，因此不把取消成功解释成“上游必定未执行”。心跳失败但数据库租约尚未过期时，也不会由旧执行者伪造终态；等权威状态允许恢复。

## 4. 缓存 Operation 双重围栏与对账

缓存 Operation 保存所属 Request 的 owner / epoch，发布、观测、结束和 create 前同时校验 Request 围栏及 Operation owner / epoch / state / lease。Request 失效后，即使 Operation 自身仍有 lease，也不能发布资源。

`unknown` 或已失效的 `running` create 不能重新 POST。仅当操作账本已持久保存精确合法的 `cachedContents/...` 句柄时，才允许同一 Session 的有效执行权领取新的对账 epoch，GET 验证原句柄并发布资源；保留操作历史及对账事实。没有句柄、GET 无法证明有效或返回身份不一致时，继续封闭，不猜测资源名、不扫描创建、不重建。此处是有条件的恢复能力，不是对未知远端副作用的一般自动消除。

## 5. 真正消费不可变配置与版本化有效身份

`ImmutableConfigSnapshot` 携带冻结连接定义和不可变 RouteCatalog，RouteResolver 实际消费该快照。目录本体、条目集合及嵌套合同不可原位修改。旧 Session 的路由、能力、协议和缓存合同继续使用自己的冻结事实；配置更新不替换它们。

新缓存合同 Request 使用 `relay-request/2.4`：身份和幂等比较使用冻结能力解析出的有效 think / options 语义，固定开启模型的 low / high 等标签可以收敛；原标签仍留在 request.json 供审计。显式 on / off、输入文本、材料顺序、执行方式和缓存控制仍区分。已有 2.1 / 2.2 / 2.3 身份算法及 2.3 原始调用意图比较不变，不批量重算历史 hash。

受理只冻结 CacheIntentPlan 的候选、条件、策略和授权分支，不读取当前 ready 库存或近期复用统计来改变逻辑身份。复用率、TTL 预测和实时经济性证据移到运行时仲裁，写入运行时副本和执行绑定，不回写冻结计划。原生 JSON、材料和消息顺序投影规则保持已有语义。

## 6. 上游实际行为的合同检查

供应可在 `metadata.execution_authorization` 中冻结下列授权；仅控制面可发布，调用方参数不能覆盖：

```json
{
  "allow_provider_fallback": false,
  "allow_paid_fallback": false,
  "paid_fallback_budget_ref": null,
  "allowed_actual_models": [],
  "expected_billing_tier": null,
  "on_unknown_billing": "audit",
  "require_native_constraint_evidence": false
}
```

免费转付费的明确观测必须满足付费 fallback 授权及非空预算引用；授权付费 fallback 同时必须允许 provider fallback。冻结预期为 free 而观测为 paid 时同样受此约束。实际模型继续按冻结 `observed_model_policy` 核对，显式允许的模型别名由 `allowed_actual_models` 给出。协议明确降级、原生约束被明确移除或 JSON 被修复时，不能当作原生约束合同成功。

额外计费、约束执行和协议降级证据仅从 **已认证并冻结的 ProtocolProfile.metadata.response_evidence_headers** 映射取得，支持 `paid_fallback`、`billing_tier`、`native_constraint_enforced`、`protocol_downgraded`；原有 AIHubMix routing / JSON repair 观测继续保留。没有可信字段时记录 unknown；`require_native_constraint_evidence=true` 或 `on_unknown_billing=reject` 可要求相应证据，否则保持审计语义。符合 JSON Schema 不等于证明 Provider 执行过原生约束。

上述检查发生在原始 2xx 响应归档之后。合同失败保留 raw / output 对象，单次 Provider 调用不因此重发。本版不声称可凭响应推断完整账单，也不把预算引用当作已核实的实际扣费金额。

## 数据库、部署与回滚

1. 从已有 7.0.0 数据库（001–010 已执行）升级，暂停新受理、排空在途任务并停止旧 Worker。
2. 执行 `sql/011_execution_authority_closure.sql`。新增 Operation 关联列并替换最终执行权 RPC；不删除业务表、不重算历史 Request 身份，不重写已有 Session / CachePlan。空白库按 001–011 顺序执行。
3. API / Worker 同批部署本包，版本均为 7.1.0，连接同一数据库并加载同一已发布控制面文件。旧 Worker 不应与本版混跑，不可消费新 2.4 计划。
4. 恢复受理后，分别验证同步与异步成功、同 key 重放、发送前失租、封存后失租，以及 `result_stored` 只提交恢复。上游验收范围见验证报告。
5. 回滚前停止新受理并排空本版新请求；2.4 请求及新执行事实必须由支持它们的节点完成或保留处理能力。旧代码不认识 2.4，不可仅替换镜像后让旧 Worker 领取遗留任务。不要通过删除迁移列或批量改写身份来回滚。

完整代码差异及文件摘要见 `CHANGE_MANIFEST_7.1.0.json`。本次本地验证已完成，原生 PostgreSQL 多连接竞争和真实渠道验收仍需在部署环境执行；具体已验及未验项见 `VALIDATION_7.1.0.md`。
