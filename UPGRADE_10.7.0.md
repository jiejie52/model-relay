# Model Relay 10.7.0

基于《架构机制说明》与《续接改造方案》，在保持现有四层职责边界与 `material-placement/2` + `relay_continuation_anchors` 账本不变量的前提下，本版本闭环四项续接关键缺口：

## 变更摘要

### 1. OpenAI / Grok Responses 树状锚点与含材料增量续接（P1-1）
- [`app/protocol_adapters/wire_projection/history_capabilities.py`](app/protocol_adapters/wire_projection/history_capabilities.py) 与 [`app/control_plane/definitions.py`](app/control_plane/definitions.py)：为 `responses` 注册 `("input_file", "input_image", "input_text")` 原生历史材料复用能力，并在 `responses-official-v1` 启用 `supports_native_history_material_reuse=True`。
- [`app/runtime/material_binder.py`](app/runtime/material_binder.py) 与 [`app/execution_plane/material_reachability.py`](app/execution_plane/material_reachability.py)：当 `responses` 等无上游文件适配器通道运行于 `material-placement/2` 时，按冻结 `account_scope_id` 严格写入/复用 `provider_material_bindings`（`responses_inline_text`、`responses_inline_data`、`responses_external_url`、`relay_structured_text`），完整沿用 SQL 014/016 双层封存校验与 `relay_material_pins` 资源保护。
- [`app/protocol_adapters/wire_projection/responses_wire.py`](app/protocol_adapters/wire_projection/responses_wire.py)、[`app/runtime/physical_context.py`](app/runtime/physical_context.py)、[`app/runtime/material_history.py`](app/runtime/material_history.py)、[`app/core/material_placement.py`](app/core/material_placement.py)、[`app/execution_plane/result_committer.py`](app/execution_plane/result_committer.py)、[`app/observability/material_metrics.py`](app/observability/material_metrics.py)、[`app/protocol_adapters/profiles/responses_v2.py`](app/protocol_adapters/profiles/responses_v2.py) 与 [`app/protocol_adapters/profiles/openai_compatible.py`](app/protocol_adapters/profiles/openai_compatible.py)：支持在 `user_item.content` 内按官方 Responses 规范（`input_text` / `input_image` / `input_file` `file_data`+`filename` 或 `file_url`）组装当轮与 `Delta` 增量历史材料，完整记录并核验 `part_digest` 与多维材料指标，避免增量续接时重复前置 `material_prefix`。

### 2. `parent_head_fingerprint` 断链重开与策略控制（P1-2）
- [`app/v2_repository.py`](app/v2_repository.py) 与 [`app/runtime/continuation_binder.py`](app/runtime/continuation_binder.py)：`list_continuation_prefixes` 取消硬过滤 `head_fingerprint`，改由 `ContinuationBinder` 审计并记录 `head_fingerprint_mismatch`；若无匹配当前头的合法锚点且存在异头活跃锚点：
  - `allow_head_mismatch_reopen=False` 时无论 `prefer_stateful_anchor` 或 `require_stateful_anchor` 一律 Fail-Closed 拒绝（`CONTINUATION_HEAD_MISMATCH_REOPEN_FORBIDDEN`）；
  - `allow_head_mismatch_reopen=True` 时使用 `decision_reason="head_fingerprint_diverged_reopen"` / `fallback_reason="HEAD_FINGERPRINT_MISMATCH"` 无损断链重开新根链。
- [`app/protocol_adapters/wire_projection/gemini_interactions_wire.py`](app/protocol_adapters/wire_projection/gemini_interactions_wire.py) 与 [`app/protocol_adapters/profiles/openai_compatible.py`](app/protocol_adapters/profiles/openai_compatible.py)：下发 Wire `tools` 前执行与 `compute_parent_head_fingerprint` 对称的规范化；同步修正 [`UPGRADE_10.2.1.md`](UPGRADE_10.2.1.md) 文档描述。

### 3. 控制面启动部署探针续接实测与门禁（P2-1）
- [`app/control_plane/governance/workbook.py`](app/control_plane/governance/workbook.py)：`COMPILER_VERSION` 升至 `relay-excel/10.7.0`，编译阶段 `continuation.verification_status` 默认保持 `'unverified'`。
- [`app/control_plane/governance/deployment_validation.py`](app/control_plane/governance/deployment_validation.py)：为 `gemini_interactions` 与 `responses`（支持 `implicit_prefix` 及续接）补齐两轮真实 `store=True` + `previous_interaction_id`/`previous_response_id` 召回实测与 `DELETE` 清理（代理返回 `404`/`405`/`501` 记 warning 不阻塞认证），通过后产出 `stateful_continuation` facet。
- [`app/control_plane/auto_workbook.py`](app/control_plane/auto_workbook.py)：仅在探针实际观测到 `stateful_continuation` facet 后才将能力合同 `continuation.verification_status` 晋升为 `'verified'`。

### 4. 锚点配额淘汰与后台巡检回收（P3-1）
- [`app/control_plane/continuation_contracts.py`](app/control_plane/continuation_contracts.py)、[`app/execution_plane/continuation_intent_planner.py`](app/execution_plane/continuation_intent_planner.py) 与 [`app/execution_plane/result_committer.py`](app/execution_plane/result_committer.py)：统一 `max_active_anchors_per_session` 默认值为 `128` 并随锚点提交载荷传入数据库。
- 新增前向迁移 [`sql/020_continuation_anchor_lifecycle.sql`](sql/020_continuation_anchor_lifecycle.sql)：
  - `complete_continuation_context_v91` 插入新锚点后按会话淘汰最旧且无 `relay_anchor_pins`（`active`/`held`）保护的超配额活跃锚点为 `superseded`；
  - 新增 `reap_continuation_anchors_v1` 将过期活跃锚点标记为 `expired`，并在保留 `parent_anchor_id` 树依赖与在途 pin / 请求保护的前提下，安全删除超过保留宽限期的孤立终态记录（不触发远端 `DELETE`）。
- [`app/config.py`](app/config.py) 与 [`app/execution_plane/schedulers/worker_daemon.py`](app/execution_plane/schedulers/worker_daemon.py)：在 Worker 活跃周期内节流执行巡检回收，完全不影响 API 空闲且队列耗尽时的 `quiescent` 自动休眠边界。

## 部署步骤

1. 在 Supabase 按序确认已安装 `001`—`019` 后，执行前向迁移 [`sql/020_continuation_anchor_lifecycle.sql`](sql/020_continuation_anchor_lifecycle.sql)。
2. 使用统一 `10.7.0` 镜像同步部署 API 与 Worker；无公网凭证或不支持 `store=True` / `previous_*_id` 的供应将保留 `continuation.verification_status='unverified'` 并按合同 Fail-Closed。
