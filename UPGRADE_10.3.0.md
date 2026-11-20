# Model Relay 10.3.0

Context Continuation（上下文续接）架构设计增强、偏离纠正与封存漏洞修复。

## 升级亮点与核心机制增强

### 1. 彻底消除封存旁路漏洞（Dispatch Sealer Hardening）
- 彻底移除 `dispatch_sealer.py` 中历史遗留的独立 `seal_continuation_and_dispatch_v90` 分支。
- 所有启用 Context Continuation 的 v3 请求统一汇聚至 `seal_continuation_context_v91` 执行原子复合封存，确保 `install_cache_binding_v3`（上下文缓存绑定）与 `seal_materials_and_dispatch_v89`（材料哈希封存）绝对不会被旁路绕过。

### 2. 合同驱动的头部指纹升级（Head Fingerprint v4）
- 推出 `CONTRACT_DRIVEN_HEAD = "relay-continuation-head/4"`，遵循方案 2.5.1 架构不变量：
  - 将系统指令（`instructions`）与受控规范化工具声明（`tools`）正式纳入头部指纹计算，配置变更将触发可控断链与全量重放，杜绝指令与工具漂移导致的会话认知分歧；
  - 严格排除思考控制参数（`thinking` 开关与预算）以及非语义采样控制（如 `temperature`），确保客户端推理配置微调不中断连续会话；
  - 纯函数指纹计算向下兼容 `head/2` 与 `head/3` 历史锚点的只读比对。

### 3. 受理期纯函数规划与动态句柄隔离（Pure-Function Intent Planning）
- 彻底解耦 `RequestAcceptor` 受理期对数据库活动锚点的查询。请求受理阶段仅依据控制面供给合同、续接策略与入参执行纯函数规划（`plan_context_intent`），生成确定的 `continuation_intent_plan`。
- 在 `idempotency.py` 中注入硬防御断言，阻断任何上游动态句柄（如 `parent_anchor_id`、`upstream_handle`）污染 `request_identity` 与 `request_hash`，恪守幂等性架构不变量。
- 物理锚点的真实发现、亲和性校验、连续性校验统一后移至物理执行期 `ContinuationBinder` 处理。

### 4. 显式白名单准入与脏写入根除（Result Committer Alignment）
- `ContinuationIntentPlanner` 改用显式协议白名单准入（当前稳定支持 `gemini_interactions`），非白名单协议直接降级为普通全量交互。
- 彻底删除 `ResultCommitter.finalize` 中历史遗留的 `elif not is_interactions` 盲目写入分支，不再为未返回有效句柄的请求生成伪锚点。
- 泛化锚点落库校验：仅当 `topology.reusable_on_commit`、`actual_binding.store_upstream_state` 成立且上游明确观测到持久化事实（`upstream_state_stored` / `interaction_stored` 为 True 且 handle 一致）时，方调用 `_build_committed_anchor` 写入有效锚点。
- 泛化 `recover_and_commit` 中的封存比对，解除单协议绑定。

### 5. 三类锚点形态（Anchor Classes）与闭环校验
- 显式建模三类锚点分类：
  - `stateful_session_handle`：长效服务端会话句柄（如 Gemini Interactions）；
  - `ephemeral_turn_handle`：单轮瞬态续接句柄；
  - `stateless_prefix_cache`：基于无状态前缀缓存的续接。
- 执行期执行严格的线性连续性检查（`linear_continuity`）、亲和代次联动淘汰（`affinity_epoch`）以及跨轮次材料覆盖闭环核验（`verify_parent_chain_material_coverage`），并在候选排除时记录完整的 `rejected_candidates_audit` 审计跟踪。

### 6. 零数据保留（Zero Data Retention, ZDR）安全门禁
- 在意图规划期增加 `enforce_zero_data_retention` 强约束：若会话或请求处于合规 ZDR 要求下且策略未获得显式豁免，强制 Fail-Closed 禁用有状态服务端续接，杜绝敏感会话状态在第三方服务端持久化。

### 7. 执行时序规范化与重开降级泛化
- 统一 `RelayRuntimeEngine` 执行时序：确保 `MaterialBinder` 严格在 `ContinuationBinder` 之前执行，使续接校验具备完整的材料绑定上下文。
- 泛化上游 400/404/410 锚点失效识别，涵盖 `previous_response_id` 与主流厂商通用失效错误信息；在重开降级重试时调用通用的 `reproject_for_continuation`。

---

## 升级说明

1. **服务升级**：从 10.2.1 同步升级 API 和 Worker 服务，无需调整既有数据库结构，继续沿用 SQL 016。
2. **平滑兼容**：
   - 存量未决请求与历史 `relay-continuation-anchor/2` 记录完全兼容。
   - 升级后新发起的请求将采用 `relay-continuation-head/4` 与纯函数意图规划。已有会话首次使用新规则时将安全回退全量首轮建立新锚点，随后自动进入增量续接。
