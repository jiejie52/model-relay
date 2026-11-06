# Model Relay 6.6.0 缓存补丁与 Gemini 字段顺序

本版合入《改造方案补丁1》及 systemInstruction 顶层首字段要求。交付代码从上传的原始源码副本重放前次升级修改，再叠加本次补丁。

## 首轮协议物化

同账号、同模型供应族中的 gemini_native 与 chat_completions 画像在 Session 创建时形成冻结候选集合。Session 携带附件或 workload_hint 为 multimodal_or_long_doc 时直接选择 Native。pure_text_qa 优先选择具备资格的 Chat 画像。auto 且没有附件时，在双画像均具备资格的供应族内进入 pending_first_request_pin。

首轮请求携带附件，或在累计历史模式要求 on 时选择 Native。单轮共享前缀模式或带 instructions 的纯文本请求优先选择 Chat，并校验实际模型选项和结构化输出能力。协议物化与 Request 受理由同一数据库事务完成。并发竞争、资格不符或受理失败不能留下半完成的路由冻结。

内置分组覆盖原目录已登记的 Gemini 3.1 Flash-Lite 和 Gemini 3.8 Flash。其他模型可在已发布控制面中通过 offering.supply_group_id 声明两条同账号供应画像。其能力、模态、协议与账号仍需分别校验。

## 单轮共享前缀上下文

原有 context_policy 字符串继续可用。新 Session 可以提交业务语义对象，例如

```json
{
  "history_mode": "ephemeral_turn",
  "sticky_scope": "session",
  "workload_hint": "auto"
}
```

该对象作为 context_policy 的值。ephemeral_turn 请求保存完整 Request 结果及用量，但每轮只向上游发送固定系统指令、允许的共享材料和本轮问题。成功提交仅释放 active_request_id，history_version 与历史对象保持原值。后续请求省略 instructions 时继承首轮受理的 instructions。

sticky_scope 为 conversation_family 时，会话令牌绑定租户、conversation_hash、具体供应、账号、协议、系统指令语义摘要和亲和 epoch。Relay 管理的显式缓存资源继续按 Session 隔离。

## 缓存与发送规则

- Chat 画像在首次派发前对稳定文本前缀执行同模型 countTokens 计数。低于冻结门槛时记录 insufficient_prefix_length，并发送完整普通推理请求。计数失败记录未知资格，auto 仅在原冻结策略授权下使用无缓存分支。实际命中以 Provider usage 为依据。
- 显式缓存只覆盖合法稳定前缀，保留末轮非空用户问题。允许缓存工具的 Native 合同把 tools、toolConfig 与 systemInstruction 一并放入资源创建请求。generateContent 仅发送句柄、未缓存尾部、超预算附件及生成参数，记录 payload_stripped_bytes。
- 原始 User Content 内的附件顺序与引用关系继续受到语义等价约束。请求临时附件不能仅因选择了 Native 就被单独抽离到缓存中。可独立投影的 Session 共享材料使用既有 60 MiB 预算。
- Native 画像仅根据未过期显式句柄的 403/404 证据考虑亲和淘汰。Chat 画像根据路由器明确断粘或可比较的连续 warm miss 处理。缺少路由响应头或缓存计数属于未知事实。
- 滚动快照门槛为 T_delta 达到 alpha×T_old 或 Tcap，且不少于最小增量。默认 alpha 为 0.5、Tcap 为 16384、最小增量为 2048 token。通过门槛后仍需满足冻结价格、预算和净收益条件。
- 统一 observed_cache_audit 输出两类协议的缓存 token、命中比例、模态明细、路由观测和瘦身字节数。缺失或非法计数保持 null。
- gemini_native.py 在最终发送前把存在的 systemInstruction 移到顶层首位。HTTP 规范化也保留该位置，其余顶层字段和嵌套对象继续确定性规范化。显式缓存推理按资源合同省去顶层系统与工具字段。

## 部署

1. 已部署 6.5.0 的数据库执行 sql/009_cache_v66_patch.sql。迁移沿用现有 UUID、对象引用、资源表和 fencing 机制。文件编号按现有迁移序列递增。
2. 同时更新 API 和 Worker，确认版本均为 6.6.0。新的受理、结果保存和完成 RPC 分别为 accept_relay_request_v66、store_relay_result_v66、complete_relay_request_v66。
3. 采用新 Session 验证延迟协议物化和新的 context_policy 对象。既有 Session 按其原有冻结供应处理。
4. 对实际供应验证 countTokens 接口、工具缓存、usage 和账单。粘性头及本地通过的投影验证均不能保证每次 Provider 命中。

本地验证覆盖真实 v2 受理入口、共享执行运行时、SDK 与 HTTP 模拟上游、30 MiB PDF 瘦身、字段顺序以及 PostgreSQL 迁移、事务回滚、执行权校验和 ephemeral_turn 提交。真实 AIHubMix 与生产 Supabase 验证尚待部署执行。P3 心跳和后台资源清理沿用此前范围。
