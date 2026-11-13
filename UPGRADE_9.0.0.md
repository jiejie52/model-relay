# Model Relay 9.0.0 — Request 级 context_policy 上下文可见范围

## 基线、版本与范围

唯一源码基线：用户此前上传的 **Model Relay 8.9.1 原始完整源码包**。先完整复制基线，再仅修改本需求必要代码；原始源码包不改动。此变更是**新能力需求**，按 `XX.YY.ZZ` 规则从 `8.9.1` 升级为 **`9.0.0`**。

**本版不引入 `include_history` 字段，不引入小模型/RAG，不修改数据库结构、SQL 迁移、Provider 路由或 Session 创建合同。** Session 创建仍使用原有的 `context_policy="conversation"` 或 `"explicit"`（以及现有结构化 `history_mode`）；新增的是 **每个 Request 上复用同名 `context_policy` 的可选覆盖字段**。

## 公开请求合同

对 `POST /v2/sessions/{session_id}/requests`，新增可选顶层 `context_policy`：

| Request `context_policy` | 本轮模型可见的已提交历史消息 | Session 固定锚定材料 | 本轮 `material_ids` | 成功后 Session 历史累计 |
| --- | --- | --- | --- | --- |
| 省略 | 继续按创建 Session 时的原策略 | 继续按 Session 原策略 | 是 | 保持原策略 |
| `conversation` | 按累计 Session 原策略继承 | 是（限 conversation Session） | 是 | 保持原策略 |
| `explicit` | 否 | 否 | 是 | 原 Session 是累计模式时继续累计 |
| `session_materials_only` **新增** | 否 | 是 | 是 | 原 Session 是累计模式时继续累计 |

说明：Request 级覆盖**不会改变 Session 冻结的 `context_policy`、`history_mode`、RouteBinding、原生历史或提交版本**。创建时已设为 `explicit` 的 Session 不允许通过单次 Request 提升为 `conversation` 或 `session_materials_only`（返回 `422 CONTEXT_POLICY_OVERRIDE_NOT_ALLOWED`）。`history_mode="ephemeral_turn"` 本来就不累计历史，本次覆盖不会强行改变它。

`adaptive` 仅为远期可能的策略名称，**本版不支持**，传入时按枚举校验拒绝。

## Fusion 调用示例

1. 创建 Main Session（公共原始材料锚定，候选答案不放入 Session manifest）：

```json
{
  "tenant_id": "<tenant>",
  "conversation_hash": "<conversation>",
  "provider": "gemini",
  "model": "<model>",
  "context_policy": "conversation",
  "material_ids": ["<common-source-material-id>"]
}
```

2. 审议阶段，在本轮 Request 显式附加候选答案：

```json
{
  "input": {"stage": "DIRECT_ADJUDICATION", "stage_instruction": "<adjudication-instruction>"},
  "material_ids": ["<candidate-material-id>"],
  "execution": {"mode": "async"}
}
```

3. 最终融合成文，在**同一个 Main Session** 中仅继承公共固定材料，不继承候选审议历史：

```json
{
  "input": {
    "stage": "DIRECT_FINAL_SYNTHESIS",
    "stage_instruction": "<final-writing-instruction-and-approved-decision>",
    "stage_payload": {"effective_brief": "<verified-effective-brief>"}
  },
  "context_policy": "session_materials_only",
  "material_ids": [],
  "execution": {"mode": "async"}
}
```

注意：`stage_instruction` / `stage_payload` 必须显式提供最终成文所必需的有效裁决、任务边界与融合方案等内容；本版**不自动从被跳过的历史中提取这些信息**。如调用方再次把候选答案写入当前输入，Relay 不会替用户做文本内容过滤。

## 实现要点与架构不变量

- RequestAcceptor 解析单轮可见范围，冻结在 Request snapshot、ContextPlan、CacheIntentPlan 和请求身份中。没有传参时保留旧行为和旧幂等判定。
- Material Placement 始终使用完整已提交历史校验原生材料发生账本。`session_materials_only` 中禁用从**模型不可见历史**继承材料的省略优化，保证公共锚定材料在本轮真实投影；`explicit` 模式只保留本轮显式材料。
- 运行时将完整持久化历史与模型可见历史分离；Gemini 原生 Layout/10 中历史账本验证仍用完整历史，模型 wire 使用本轮筛选后的视图。其他 Adapter 同样收到已筛选的模型历史。
- 历史提交器仍以 **原始完整 Session 历史** 追加成功的 Request，原 Session 为 `cumulative` 的情况下本轮跳过历史也不会清空或覆盖此前轮次。
- RouteBinding、连接身份、控制面冻结、ExecutionFence、AtomicDispatchSeal、Cache Fingerprint 和 Provider 原生历史结构保留已有校验。未改 SQL/数据库结构。

## 已知兼容边界

1. **历史材料 Binding 校验仍然严格执行。** 因为 8.9.1 的 `material-placement/2` 与 SQL 014 在发送前要求对历史原生文件 Binding 进行安全校验，模型不使用的历史附件如果已经过期，仍可能导致失败。为保持原有 SQL/封存不变量，本版未放宽或跳过此校验；后续如需让过期历史材料完全不影响精简视图，应另立经审核的迁移需求。
2. 8.9.1 中针对“每轮材料自动锚定”的材料复用只在真实继承模型历史时成立；跳过历史时公共材料会重新作为当前模型输入投影。不能以 `material_ids=[]` 推断 Session 锚定材料不再出现。
3. 兼容旧 Request 的快照与身份算法；旧的执行中 Request 按冻结快照恢复，而不是根据新的策略重新选择上下文。
4. 本版不支持选择历史的任意片段，也不自动排除当前输入或 Session 锚定材料内部的候选文字。只实现 `conversation` / `explicit` / `session_materials_only` 三类本轮可见范围。

## 部署与验证

- **无新增 SQL、无数据库迁移、无依赖升级。** 保留原先安装的 `001`—`014` 迁移。
- API 和 Worker **必须一起部署 9.0.0**；避免旧 Worker 以旧的模型可见范围处理新 Request。
- 上线前用全新 Fusion Session 验证：公共锚定材料、候选答案请求材料、审议结果、Final Writer `session_materials_only`、继续正常累计下一轮、幂等相同 Key 恢复，以及 `requested_cache_mode=off/auto` 的物理投影一致性。
- 本地只执行静态编译、schema/身份和材料账本/原生上下文的隔离模拟，**未执行真实 Gemini、生产数据库 SQL、Worker 租约/故障注入、Dify 端到端业务验收**。因此需在受控环境通过真实调用再部署生产。
