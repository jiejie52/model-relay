# Model Relay 7.0.0 架构职责优化

以用户上传的 `model-relay-6.8.0_20261108(1).rar` 为唯一源码基线，完整复制后修改。版本按“新的架构修改需求”升级为 7.0.0。原上传包未修改。

## 职责与实际调用链

| 层次 | 主要实现 | 职责 |
| --- | --- | --- |
| 控制面 | `app/control_plane/` | 静态模型和供应声明、能力与缓存合同、资源适配器登记、离线编译与只读发布快照 |
| 执行面 | `app/execution_plane/` | 会话与请求受理、资格过滤与选路、有效语义解析、上下文和统一缓存计划、同步异步调度、合同校验与结果提交 |
| 运行时 | `app/runtime/` | 执行权核验、材料绑定、物理上下文、只读缓存评估、集中仲裁、选定资源物化、原子发送封存与执行事实归档 |
| 协议适配层 | `app/protocol_adapters/` | 封存输入的原生协议投影、单次协议调用、响应解析、Schema Wire 投影与用量归一化 |

`api_v2/router.py` 的 Session / Request 写入处理器只负责 HTTP 入参、鉴权依赖、领域错误映射与响应序列化。受理流水线进入 `SessionManager` / `RequestAcceptor`；调度器装载 `FrozenExecutionPlan`，调用 `RelayRuntimeEngine`，再由 `ResultCommitter` 校验和提交。

`RelayRuntimeEngine` 不再完成 Canonical Schema 后置校验、公用 compact_result 组装、历史生成或 Session 终态事务。材料、物理上下文、缓存评估与物化、发送封存、单次调用分别有独立实现。`CacheRuntimeArbiter` 先只读评估与集中仲裁，`CacheMaterializer` 才落实选定分支；资源评估不再写入失效状态。

供应组的静态扩展留在控制面；依据首轮请求选择供应及冻结路由的逻辑移到 `execution_plane/supply_resolver.py`。缓存意图、物理缓存绑定、用量观测和执行权契约按实际职责拆分，没有把混合文件整体归入控制面。纯内容编解码与 Canonical Schema 值对象作为共享工具使用，控制面不反向导入执行面服务。

## 层间契约与恢复

- 新增 `ImmutableConfigSnapshot`、`FrozenExecutionPlan`、`SealedDispatchBundle`、`ActualExecutionFact` 和 `StandardizedProviderResult`。跨层请求、绑定与结果采用冻结 dataclass 和递归只读 JSON 容器，保持 dict/list 的现有协议序列化行为。
- API 与 Worker 启动验证已发布快照的协议实现兼容性；Worker 在领取任务前再次验证。既有会话和请求继续消费自身冻结事实。
- 原子发送仍使用现有受控 RPC 封存绑定、pin 与 Adapter 输入摘要。运行时和五个原生协议入口均验证封存输入；摘要范围仍为 Adapter 执行输入，不扩大宣称为最终 HTTP 字节原子封存。
- 成功响应先归档原始实体、输出和 `execution-fact.json`，随后写入 `result_stored`。`provisional_compact_result` 仅保存受控执行事实对象引用；执行面再做 Canonical 校验并原子提交。
- 新格式的 `result_stored` 恢复只读取已保存对象，核验请求和原始响应摘要，完成校验与提交，不进入运行时、缓存或 Adapter。旧格式已组装好的 provisional result 仍按原有格式恢复。
- 同步请求新增独立的 commit-only fence。执行权有效时不会被其他恢复者抢占；原执行权退出后，调用方可使用原 Idempotency-Key 重放相同 POST 完成只提交恢复，不新增 Request、不再次推理。GET 查询保持只读。
- 模型发送后断连，包括已收到 2xx 头但响应流中断，仍为 `indeterminate`。确定的 Provider 错误不会触发去缓存重发或换渠道；失租执行者不能写入结果或失败终态。

## 材料与协议

新 Session 冻结材料传输规则：默认总材料字节不超过 `gemini_files_threshold_bytes`（99 MiB）时使用 External URL 路径，超过时走 Files API 路径；具体表示仍须满足合法投影和供应合同。既有 Session 保留原先冻结的传输解释与已安装绑定。缓存内联安全预算仍独立为 60 MiB，退出缓存的材料继续保留在完整推理上下文中。

五种协议入口统一消费封存包并返回标准化结果，原始响应和历史续接内容继续保留。Claude 用量总输入按未缓存输入、缓存读取与缓存写入求和；实际零值与未提供计数分别记录。原生结构化输出合同遇到上游明确报告的 JSON 修复时，执行面按合同失败处理并保留原始成功响应；不会从有效 JSON 反推上游一定执行了原生约束。

旧源码路径保留导入或启动入口转发，实际实现归属新分层目录；API 路径、请求/响应模型、环境变量及 `python -m app.worker` 启动方式继续可用。内部运行时接口改为冻结计划与执行事实，旧类名导入不代表旧内部调用参数仍被接受。

## 数据库与部署

1. 在现有 009 数据库基线之后执行 `sql/010_execution_plane_commit_recovery.sql`。该脚本新增同步只提交执行权，并加强失败终态写入的有效租约检查，不删除业务数据或改写既有请求。
2. 停止新受理，排空在途任务并停止旧 Worker。API 和 Worker 作为同一个升级单元部署本包，再恢复受理。不要让旧 Worker 消费新格式的待提交执行事实。
3. 旧会话可继续使用冻结合同；需要采用本次冻结的新材料阈值时创建新 Session。已有请求恢复沿用自己的原快照和绑定。
4. 回滚前先用本版完成所有新格式 `result_stored` 的校验提交，或维持能处理该格式的执行节点。旧 Worker 不能直接解释新的待提交事实标记。

## 已执行验证与范围

完成全部 Python 源码编译和全模块导入；完整 OpenAPI 与基线逐项比较，仅发布版本号变化；发布目录内容、控制面 hash 与路由目录 hash 均与基线一致。离线配置 CLI 校验原工作簿通过。

本地内存存储、仓库替身和模拟 HTTP 覆盖同步/异步成功、原始事实先落盘、只提交恢复、Schema 失败、失租阻断、发送后断连、2xx 响应流中断、Provider 缓存错误不重发、提交失败后的恢复，以及五种协议的完整无缓存上下文。另核验缓存评估先于资源物化、新旧材料阈值边界和真实零/未知用量。新增 SQL 已通过 PostgreSQL 语法解析。

未执行真实 PostgreSQL RPC、AIHubMix、Gemini 或 Moonshot 生产链路验收；模拟验证不代表真实缓存命中、费用或生产故障注入已通过。未新增 tests 文件，也未改动依赖清单。代码包内提供文件变更和 SHA-256 清单。
