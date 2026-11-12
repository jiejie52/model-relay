# Model Relay 8.9.0 Session 材料位置治理

唯一基线为用户上传的 model-relay-8.8.2.zip。原始包保持只读，完整复制后修改。此次纠正既有 Session 材料自动附加的处理方式，按 XX.YY.ZZ 规则升级 YY 位为 8.9.0。原文件与隐藏文件全部保留；没有新增 tests 文件，没有修改 requirements.txt、Dockerfile 或 Excel 工作簿。

## 本版行为

对具备原生历史材料复用声明的新 conversation / cumulative Session，首轮自动附加 Manifest 材料；成功提交后，后续轮次传 material_ids=[] 时通过可信原生历史继承，不再在当前 User Content 自动重复注入。显式传 material_ids=[M1] 仍保留本轮重提。已提交的历史消息、原生 Part 顺序、用户正文、冻结路由、账号作用域和思考合同不改写。

外部 Dify JSON 接口不新增参数，不要求调用方维护 anchor_once、缓存句柄或 Provider 参数。支持能力来自冻结 ProtocolProfile，当前已注册 Gemini Native 历史位置接口；其他协议默认保持原行为。Excel 编译器版本升为 relay-excel/8.9.0，触发新的配置身份。工作簿文件本身保持原字节；既有认证门禁继续生效，不因支持历史复用而自动把 pending 供应变成 ready。官方声明示例也明确登记该能力。

## 四层职责

- 控制面：ProtocolProfile 增加 supports_native_history_material_reuse 和 history_material_part_kinds，并校验其对应已注册的历史投影能力。
- 执行面：纯函数规划器区分 Session 自动候选、当轮显式材料和历史已提交发生，冻结 material-placement/2；请求身份增加受理时冻结的材料四元组，旧身份算法保持不变。受理重放先比较原快照，不依据最新历史或灰度开关重算旧 Request。
- 运行时：本轮增量绑定与历史核验分开；历史材料核验内容、连接、作用域、代次、URI 和剩余有效期。封存前核对历史、材料顺序及 Part 位置，再通过同一数据库事务建立当前与历史材料 pin 并推进 dispatch_started。
- 协议适配层：只消费封存后的输入。layout/10 按完整的历史 User/Model 轮次切片；关闭缓存时不再读取内联缓存材料或枚举缓存候选。无法保持原生文件表示的显式缓存候选不被强行转换，继续遵循原冻结计划允许的隐式或无缓存分支。

首轮不把附件从当前 User 消息中拆出；没有完整历史前缀时，保留完整正常输入。缓存 on 的既有能力认证、门槛判定及失败语义继续生效，不将未知资格冒充低于门槛。

## 恢复与资源保护

本轮投影事实写入 SealedDispatchBundle 与 execution-fact.json。ResultCommitter 在正常提交和 result_stored 恢复提交中，统一生成 CommittedMaterialOccurrence，并随历史版本更新原子追加 committed_material_ledger。归档缺失、位置事实缺失或与封存事实不符时拒绝提交，不重发模型。

历史 fileUri 过期、撤销或代次变化，在 not_sent 阶段阻断并返回 HISTORICAL_MATERIAL_BINDING_EXPIRED。请重新上传并创建新 Session，或采用另行审查的受控迁移；本版不会静默修改旧历史 URI。

SQL 013 增加材料 pin 表和保护触发器。成功提交释放 pin；indeterminate 与无法证明上游停止的取消继续持有 pin。仍无通用的未知上游执行自动对账服务，不能通过强删 pin 或修改派发状态把未知执行变成可重发。

## 部署顺序

1. 先在 Supabase 执行 sql/013_material_occurrence_placement.sql，已有 001—012 迁移须已安装。
2. 使用同一份 8.9.0 代码部署 API 与 Worker，加载一致的配置快照。不要让旧 Worker 消费新合同请求。
3. 默认 MATERIAL_PLACEMENT_MODE=enforce_new_sessions。API 和 Worker 使用一致配置。新会话自动启用，旧会话不被自动补造凭证或改写合同。
4. 检查 API / Worker 的版本均为 8.9.0；构建可注入 BUILD_GIT_SHA，两端 health 与启动日志使用统一版本源。

SQL 013 的受理、材料封存和提交使用独立的 v89 RPC，旧 Request 继续使用原 RPC。必须先迁移再启用本版；不支持缺少 SQL 013 时降级绕过材料事务围栏。

## 灰度和回滚

MATERIAL_PLACEMENT_MODE 支持四档：

| 值 | 行为 |
| --- | --- |
| legacy_v1 | 新会话保留原行为；已经登记新合同的会话继续记录发生凭证，但本轮按旧策略重复附加自动材料 |
| shadow | 新会话记录可信发生，实际使用原材料投影与 layout/9；日志另外给出假设启用去重时可省略的数量 |
| enforce_new_sessions | 对登记新合同且协议合格的会话启用自动历史继承；未登记的旧会话继续旧行为 |
| enforce_all_verified | 在协议声明与历史凭证完整的前提下扩展启用，不补造旧会话的历史证据 |

影子或回滚状态下继续写入发生账本，是为了避免重新启用时出现历史凭证断层。其跟踪合同仍为 material-placement/2，实际材料投影保持旧策略；这是内部记录，不改变 Dify 传参。

已受理 Request 永远按其冻结计划执行，开关只影响后续受理。回滚应使用保留 v2 解释器的本版代码并切换开关，不要回退到不能解释新快照的旧二进制。历史中曾经合法重复提交的附件不会被删除。

## 观测

分项日志和 execution-fact.json 包含材料候选、历史发生、自动省略、当轮绑定、历史 pin、文件 Part 数、文本字节、Canonical / 投影 Schema 字节、缓存动作及原生 Usage。wire_total_file_part_count 统计本次实际推理 contents；logical_total_file_part_count 同时记录含缓存前缀的完整逻辑上下文，避免显式缓存引用时混淆口径。未知 Token 字段保持 null，不用字节数推算实际节省。

HISTORICAL_MATERIAL_SAFETY_SECONDS 默认 120，并随材料计划冻结；数据库以自身时钟复核。

HTTP Wire 仍通过 RELAY_HTTP_WIRE_LOG_PROVIDERS 的模型 glob 启用，256 KiB 阈值保持固定。HTTP_WIRE_BODY_AUDIT_MODE 默认 redacted_structure，只保留结构、长度与摘要。受控排障可设为 full_hex_debug，同时必须提供一小时内的 HTTP_WIRE_FULL_DEBUG_UNTIL UTC 时间；到期自动停止完整 Body 记录并告警。Body 调试不改变独立的原始响应和错误归档合同。

## 本地验证与生产验收边界

| 验证范围 | 结果 |
| --- | --- |
| 源码编译、API / Worker 导入、工作簿编译 | 通过；使用原 requirements.txt 的固定直接依赖版本 |
| 材料规划、历史继承、显式保留、范围隔离、损坏 / 过期拒绝、只提交恢复、缓存前缀和日志脱敏 | 41 项检查通过 |
| 与原包历史身份和物理布局逐项对照 | 100 组 Request 身份、8 种旧布局及旧 Profile 序列化共 109 项检查通过 |
| 实际 RequestAcceptor / PlanLoader 与冻结重放 | 12 项检查通过 |
| 实际封存器、Gemini Adapter、HTTP 传输、执行事实归档及 500 零重发 | 13 项检查通过；上游使用受控 HTTP 替身 |
| 001—013 迁移与数据库事务、pin、过期拒绝、旧执行者拒绝和恢复提交 | 本地 PGlite PostgreSQL 执行共 40 项检查通过，其中含 13 个迁移；仅省略 PGlite 不提供的 pgcrypto 扩展声明，UUID 使用引擎内置函数 |

校验脚本和依赖仅用于临时验证，未装入代码包。上述结果不代表真实 Supabase 多连接竞争、Google 账号认证、费用对账、Docker / Railway 部署或 Dify 业务回归已完成。

正式生产验收仍需：使用同一材料、有效 fileUri、指令、Schema、模型和 cache_mode=off，比较 C 首轮 Global、A 旧 Finalize 和 B 新 Finalize；保存 promptTokenCount、promptTokensDetails、cachedContentTokenCount。A-B 才是本案重复材料的实测净开销。本文未使用模拟 Usage 宣称真实 Token 节省率。还需完成实际 Global → Decision Lock → Final Writer → G01—G10 → Quality 回归，以及缓存真实命中、有效期和故障注入验收。
