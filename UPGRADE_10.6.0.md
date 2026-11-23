# Model Relay 10.6.0

本版本针对 `mutable_session_container` 的状态污染逃逸窗口实施 P0 闭环修复，在不改变 Control Plane 与 Execution Plane 既有架构不变量（四层单向契约分离、`request_hash` 不含动态续接句柄、不可逆派发边界 `not_sent` vs `dispatch_started`、`mutable_session_container` 禁用分叉与多轮增量）的前提下，增强 Runtime 与 PostgreSQL 账本约束：

1. **派发越过边界后的 `mutable_session_container` 自动污染失效（SQL 018）**
   - 在 `release_continuation_after_request_v91()` 触发器中，当绑定了 `mutable_session_container` 锚点或 `store_upstream_state = true` 的请求已越过 `dispatch_started` / `result_stored` 后转入 `indeterminate`、`failed`（含 `ResultCommitter` 后置 Schema/Thinking 校验失败导致的 `result_stored -> failed`）或 `cancelled` 时，原子将当前 Session 下 `active` 的可变容器锚点标为 `invalidated` 并记录明确失效原因；
   - 同步补齐 `cancelled`（派发后取消）时的 `relay_anchor_pins` 持有状态 `held`。

2. **非累计/无状态轮次推进 `history_version` 时的线性/可变容器自动退位（SQL 018）**
   - 新增 `relay_sessions` 触发器 `retire_linear_anchors_on_history_advance_v92`：当累计 Session 因 `explicit`、`session_materials_only`、`stateless_full_replay` 或未生成锚点的完成路径推进 `history_version` 时，原子将旧版本仍处于 `active` 的 `linear_ephemeral_cursor` / `mutable_session_container` 锚点标为 `invalidated`；
   - 更新 `complete_continuation_context_v91()`：当成功提交新的 `linear_ephemeral_cursor` 或 `mutable_session_container` 锚点时，将上一轮同源祖先迁移为 `superseded`。
   - 对 `immutable_tree_anchor`（如 `gemini_interactions`、`responses`）保持完全无影响，保留树状前缀分支能力。

3. **Runtime 与数据库双重封禁与无状态兜底（Runtime & SQL 018）**
   - `RelayV2Repository.has_tainted_mutable_container()`（`app/v2_repository.py`）与 `ContinuationBinder._bind_frozen()`（`app/runtime/continuation_binder.py`）：对 `mutable_session_container` 增加会话级污染检测；当会话已受污染、存在已有历史（`len(history) > 0`）却无合法连续父锚点、或进入 `reopen` 重绑时，在 `not_sent` 阶段直接降级为 `stateless_full_replay`（`store_upstream_state = false`），禁止新建带脏风险的有状态根容器；若策略禁止静态回退则显式抛错拒绝，不发出可能污染上游状态的调用。
   - `seal_continuation_context_v91()` 在 SQL 事务锁下复核 `linear_ephemeral_cursor` / `mutable_session_container` 的 `history_version` 与 `covered_turn_count` 一致性，以及 `mutable_session_container` 的零污染与建根前置版本约束。

## 部署说明

1. 在既有 `sql/017_continuation_ledger_state_closure.sql` 之后执行 `sql/018_mutable_container_taint_closure.sql`。
2. 同步部署 `10.6.0` API 与 Worker 进程；已有 `immutable_tree_anchor` 与无状态会话行为保持完全兼容。
