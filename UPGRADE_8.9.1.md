# Model Relay 8.9.1 Provider Binding 状态合同修复

唯一源码基线为用户本轮提供的原始 `model-relay-8.9.0.zip`。原始压缩包保持只读；本版在完整复制的源码树上修改。此次仅修复 Provider Binding 状态合同与双层校验，并增加失败诊断，不改变 Request / Session / Route / Dispatch 的处理方式，按 XX.YY.ZZ 规则仅递增 Bug 修复位：`8.9.1`。

## 修改范围

### P0-1：统一 Provider Binding 状态语义

新增 `app/core/provider_binding_state.py`，作为执行面与运行时共用的 Provider-Neutral 状态合同：

- Binding lifecycle `state` 的可用值：`active`、`ready`、`processed`；保留 `ready_provider` 作为历史兼容值，因为 8.9.0 的历史校验器曾明确接受它，当前 Provider 适配器不再写入该值。
- 若 `processing_state` 有值，也必须独立满足已完成状态；允许 `active`、`ready`、`processed`，以及历史兼容值 `ready_provider`。
- Gemini 适配器在 Provider 未提供明确处理状态时可能记录 `unspecified`。仅当 Binding lifecycle `state` 已独立证明可用时，才允许该 `processing_state`（包括 SDK 枚举形式 `file_state_unspecified`）。
- `ready_provider` 同时也是 Canonical Material 的 `status` 值；本次仅为兼容旧 Binding 记录保留，不把 Material 状态写入新 Binding。
- 未知、processing、failed、expired、deleted、revoked 等非就绪状态继续 Fail-Closed。

统一合同用于 `material_history.py`、`material_binder.py`、`material_reachability.py` 与 v2 Material 响应的就绪判断。有效期、generation、URI、connection_id、account_scope_hash 和 Canonical Material 内容身份仍独立校验，不因状态通过而放宽。

### P0-2：修复 Python 与 SQL 双层校验

- Python 当前轮与历史 Binding 校验使用同一状态合同。
- AtomicDispatchSeal 的 SQL 校验增加对应的状态合同函数，并同时校验 `state` 与 `processing_state`。
- generation、URI、表示类型、连接与账号作用域、Material 内容身份及有效期门槛保持原有约束。
- 不重新选路、不切换账号、不替换冻结 generation，不重建或改写历史 URI。
- 所有校验仍在 `not_sent` 阶段、数据库原子封存推进 `dispatch_started` 之前完成。

原 `sql/013_material_occurrence_placement.sql` 不修改。新增 `sql/014_provider_binding_state_contract.sql` 作为前向迁移，`CREATE OR REPLACE` 更新既有 `seal_materials_and_dispatch_v89` RPC，并提供 SQL 状态合同函数。

### P0-3：强化错误观测

新增结构化事件 `provider_binding_validation_failed`，覆盖 Python 运行时校验与 SQL AtomicDispatchSeal 复核失败。可观测字段包括：

- `request_id`、`session_id`、`material_id`、`connection_id`
- `expected_generation`、`actual_generation`
- `binding_state`、`processing_state`
- `validation_failure_reason`、`remaining_ttl_seconds`、`failure_stage`
- SQL 复核失败时附带 SQLSTATE 与 HTTP 状态码

失败原因细分为 Binding 不存在、状态无效、Provider 处理未完成、generation 不匹配、URI 不匹配、作用域不匹配、表示类型不匹配、有效期不足及 Canonical Material 不可用。对外业务错误码保持兼容。日志不记录完整 Signed URL、file URI、API Key、Token 或文件内容。

## 架构不变量

本版不改变以下既有边界：

- 控制面、执行面、运行时、协议适配层四层单向职责分离。
- Provider-Neutral；不增加 Gemini 专属业务分支。
- Session Frozen RouteBinding 与 account_scope_id / connection_spec_hash 身份不漂移。
- 已冻结 Material Binding snapshot 与 generation 不被静默替换。
- Canonical Material 和已提交历史原生 URI 不被改写。
- AtomicDispatchSeal、ExecutionFence、Pin 与 not_sent → dispatch_started 不可逆门槛保持不变。
- 失败继续 Fail-Closed；不通过重试、放宽 TTL、跳过 SQL 校验或重新选路掩盖问题。
- Dify / 调用方接口、request_hash、seal_hash、幂等与恢复合同保持不变。

## 文件变更

- `app/core/provider_binding_state.py`：新增统一状态合同。
- `app/runtime/material_history.py`：统一校验、失败原因与结构化日志。
- `app/runtime/material_binder.py`：使用统一状态合同。
- `app/execution_plane/material_reachability.py`：使用统一状态合同。
- `app/api_v2/router.py`：Material 的 Provider ready_for 依据统一合同计算。
- `app/runtime/dispatch_sealer.py`：记录 SQL 复核错误的安全诊断字段。
- `app/config.py`：统一 API / Worker 版本为 `8.9.1`。
- `sql/014_provider_binding_state_contract.sql`：新增前向迁移。

未新增 tests 文件，未修改依赖、Dockerfile、工作簿、Dify 工作流或调用方接口。

## 部署顺序

1. 确认数据库已经安装 `001` 至 `013` 迁移。
2. 先执行 `sql/014_provider_binding_state_contract.sql`。必须先更新 SQL RPC，再部署应用，避免旧 SQL 继续拒绝 `state=active`。
3. 使用同一份 8.9.1 代码同时部署 API 与 Worker；不要让旧 Worker 消费新版本请求。
4. 确认 API 与 Worker 启动日志 / health 版本均为 `8.9.1`。
5. 观察 `provider_binding_validation_failed`。正常的 Gemini Files `state=active` 且 `processing_state=active` 应通过；处理状态未完成、generation / URI / 作用域不一致或有效期不足仍应被阻断。

本次只生成源码包和迁移文件；未连接生产数据库、未执行迁移、未部署或重启服务。部署后仍需使用真实 Gemini Files 请求完成端到端验证。

## 本地验证边界

本地执行源码语法编译、共享状态合同用例、调用点静态核对及 SQL 迁移结构检查。未执行真实 Provider 请求、生产 Supabase 并发验证、Railway 部署或 Dify 业务 Gate。无 tests 文件被加入发布包。
