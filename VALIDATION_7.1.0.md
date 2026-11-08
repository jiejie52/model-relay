# 7.1.0 验证记录

验证日期：2026-11-08。唯一比较基线：原上传 `model-relay-7.0.0-source.zip`。临时验证脚本与依赖在交付源码之外执行，按修改提示词不随包增加 tests 文件。

## 已执行与结果

| 验证项 | 结果与实际范围 |
| --- | --- |
| Python 源码编译及全部模块导入 | 当前 129 个模块均通过；包含 API、Worker 启动入口和兼容区 |
| 退休路径静态扫描 | 旧四个目录和 core execution_runtime 已删除，代码导入为零，无 `import *` 转发 |
| OpenAPI 对比 | 与 7.0.0 完整结构相同，仅 info.version 从 7.0.0 变为 7.1.0 |
| 内置控制面 / RouteCatalog hash | 与原基线一致；新增授权字段未配置时不改变既有目录身份 |
| 原工作簿离线 CLI | 校验通过，返回 unverified_draft：59 models / 61 routes / 61 caches；不表示认证或发布通过 |
| 执行及合同回归 | 21 项通过，包含真实 Python 受理、计划装载、运行时、提交器和调度路径；Provider 与对象字节存储使用受控替身 |
| SQL 围栏及故障注入 | 11 项通过，直接执行本包 SQL / RPC；其中最终写入权限矩阵含 120 个子场景 |
| 协议 Wire 模拟 | 12 组通过：五种协议及 Gemini layout/9 无缓存物理分支，分别覆盖旧快照与 2.4 快照；单次 HTTP、指令、历史双方、材料及当前输入均保留 |
| 001–011 顺序加载 | 全部成功，包含新增列、30 个函数定义及 service_role 权限设置 |
| 原包及隐藏配置文件 | 原 ZIP 摘要未变，原有隐藏配置文件逐字节保留；文件差异见变更清单 |

内置控制面 hash：`1a3268467f11fc6ddaee6fb18eb19de18a097aec10e2ebcde1d35efd22a9cc60`。

内置 RouteCatalog hash：`e7028900494139a360f0d51ed4b144f74999c6c927f73935c505bd74a6734a7e`。

### 21 项执行与合同回归

1. 同步成功原子提交；固定 Thinking ON 的 low / high 重放复用原 Request，单次调用、不创建 Job。
2. 异步受理、领取、执行及 Request / Job 成功终态。
3. Canonical Schema 失败保留已归档原始 2xx 响应。
4. Provider 缓存 400 只发送一次，错误原始实体摘要正确。
5. 已收到 2xx 后响应中断进入不确定态并保留 Session 占用。
6. 同步 `result_stored` 提交失败后，同 key 只恢复提交，不再进入运行时。
7. 异步已存结果换新 epoch 后只恢复提交，不再进入运行时。
8. 封存前租约到期阻止发送，新执行权可恢复未发送请求。
9. 封存后租约到期不调用 Adapter，进入不确定态，同 key 不重发。
10. Adapter 准备完成后、底层 transport 发送前失租，真实模拟 HTTP 调用数为零。
11. Heartbeat 返回 false 取消准备中的子任务，推理调用数为零。
12. Heartbeat 异常取消封存后的子任务，租约到期后权威恢复为不确定态，无二次推理。
13. Session、材料 ID、亲和头、ExecutionFence、ContextPlan、所有者及重复缓存投影篡改被封存完整性校验拒绝。
14. RouteCatalog 本体、集合、嵌套能力和配置快照不可变；RouteResolver 引用实际快照。
15. 实时经济性观测只更新运行时计划副本，冻结计划保持不变。
16. 2.1 / 2.2 / 2.3 身份函数向量与原始实现结果相同。
17. 新身份仍区分显式缓存 on / off。
18. 旧 2.3 请求可执行并保持原始意图冲突判定。
19. 已知未授权 fallback、付费 fallback、原生约束移除、JSON 修复及协议降级被拒绝；有预算引用的授权分支及 unknown 审计可区分。
20. 额外响应证据仅按显式冻结的 header 映射解析。
21. 冻结材料对象及内容摘要固定，实际字节变化时拒绝投影。

### 11 项 SQL 与缓存操作验证

1. 同步 / 异步分别注入空租约、过期租约、空 owner、错误 owner、错误 epoch、终止状态；材料安装、绑定安装、seal、dispatch 标记、start、两种 store、两种 complete 及 fail 共 120 个子场景全部拒绝。
2. 空租约和过期租约不能通过 renew 复活。
3. 多资源中有一个无效时不写任何 pin、不推进派发；全部有效后原子封存；再次 seal 拒绝。
4. 跨对话、跨 connection、错误 generation、未安装材料绑定时拒绝封存。
5. 在资源更新 trigger 内 `pg_sleep(0.15)`，执行权仅剩约 0.05 秒：封存末端检测壁钟过期并抛错，已创建 pin、绑定状态及 Request 派发状态全部回滚。
6. Operation 自身仍有效而所属 Request epoch 已变化时，验证、观测、结束及发布均拒绝；Operation 空租约也拒绝发布。
7. Operation start 拒绝错误 owner 及过期租约。
8. 已知句柄的 unknown create 领取新操作 epoch，仅 GET 对账并发布成功；旧响应中的 operation_epoch 不能覆盖数据库新 epoch。
9. 无句柄的 unknown create 既不能重新 claim create，也不能进入已知句柄对账。
10. 从完整 StatefulResourceManager.prepare 入口进入 unknown 分支，仅 GET 恢复，无第二次 create。
11. 已派发 Job 的 lease 变为 null 后不再领取为推理任务；Request / Job 进入不确定态，pin 置 held，Session 继续占用。

## 验证环境与明确边界

Python 使用 3.12。SQL 运行在 **PGlite 内嵌 PostgreSQL 18.6 WASM 引擎**，通过同一进程的 stdio 子进程实际执行 SQL 函数；这不是字符串模拟或仓库 boolean 替身。但它是单连接顺序执行，**不能等价于原生 PostgreSQL / Supabase 多连接竞争验收**。

当前环境无法启动独立原生 PostgreSQL 服务，未验证真实多进程锁等待、死锁、SKIP LOCKED 公平性、API / Worker 跨节点网络分区以及生产 RLS / PostgREST 行为。上述壁钟过期注入证明事务内过期回滚路径，不替代多连接资源竞争测试。

未向 AIHubMix、Google 或 Moonshot 发送真实业务请求，未验证真实缓存命中、实际账单、渠道证据字段、文件上传断连后的远端状态及第三方 SDK 在目标部署环境中的完整网络行为。ProtocolProfile 额外证据映射须由部署方根据真实已认证渠道填写；缺失时保持 unknown 或按冻结策略拒绝。

上线前应在实际环境执行：同步 / 异步两个执行者竞争，绑定 / pin 锁等待跨越 lease 到期，心跳网络分区，已存结果抢占恢复，缓存 create 断连后的已知 / 未知句柄对账，并对实际供给核实费用与 native-constrained 证据。当前交付不将这些未执行项计为通过，不宣称生产验收达到 100%。
