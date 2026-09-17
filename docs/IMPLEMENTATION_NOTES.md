# 实现说明与已知边界

## 对齐的设计不变量

1. Relay Core 仍是可靠通信层，而不是一次性 HTTP Proxy：异步 Job、Postgres durable queue、Lease/Heartbeat、Storage 外置继续保留。
2. Checkpoint + Idempotency 仍是一条双层恢复协议：有 `job_id` 优先 Resume；只有 PREPARED 且缺失 `job_id` 时才用原稳定 key 重放。
3. 逻辑 Job 幂等不等于 Provider 严格 exactly-once。上游已经受理、本地尚未提交成功时 Worker 失联，若上游没有原生幂等/对账能力，不能承诺绝不重复执行。
4. Structured Output 仍按“调用方 canonical schema → Provider wire projection → canonical validation”处理；Relay 不理解业务字段语义。

## 本次明确替换的旧策略

旧实现对错误进行归类/摘要/截断。本版对 Provider HTTP Error 改为 lossless contract：保留原始 status、headers、body bytes、request id（若上游提供）并存储 sha256/byte length。Relay 自己的参数/owner/session 冲突仍可以使用 `origin=relay` 错误码，因为它们不是上游错误。

## Kimi/Moonshot Adapter

`app/providers/moonshot.py` 采用独立官方 Endpoint Profile，不复用 AIHubMix 凭据。Adapter 负责把 provider-neutral Session Job 转成 Kimi 原生 Chat Completions wire payload，并将完整 assistant message 作为 `moonshot-chat/1` 历史记录保存。Core 不解释其中的 `reasoning_content`、`tool_calls` 等 Provider 原生字段。

模型能力不能靠一个全局 `supports_thinking` 布尔值概括。本版对 K3 的 `generation.reasoning.effort` 做显式校验；其他模型使用明确的 `generation.thinking` 透传/校验入口，旧 `think_level` 无法安全映射时直接 Fail-Closed，而不是静默换档。

## 文件/视觉材料的已知缺口

本包没有声称完成通用的“Kimi 文件上传/提取管线”。当前 Adapter 接受普通文本、已经编码的 `data:` 媒体和已经准备好的 `ms://` 引用；普通远程媒体 URL、legacy `input_image/input_file` 会明确失败。

这样做是为了避免以下错误行为：把 Supabase Signed URL 直接当成 Kimi 支持的媒体 URL、把文件抽取文本冒充完整视觉阅读、或者在失败时悄悄退化为只读文件名。若生产确实需要 PDF/DOCX/图片等材料，应新增独立 Material Preparation 组件，并为“上传/抽取/视觉完整性”分别做合同测试。

## 兼容层

`app/compat/v1_jobs.py` 与 `app/compat/dify_gateway.py` 继续承载旧入口；`app/application/` 承载 legacy Fusion runtime。它们可以依赖 Core/Provider 抽象，但 Core Worker、Core Repository、Core API 不反向依赖 Dify/Fusion。

## 可靠性增强

- request fingerprint：同 key 不同 request 冲突。
- owner-scoped idempotency：tenant + conversation + key。
- append Session 单 active job reservation：在调用 Provider 前就阻止并发历史推进。
- Lease fence：过期 Worker 不能晚提交。
- 原子 commit：Session history pointer/version 与 Job succeeded 在一个 DB transaction 中闭合。

这些是本次顺带补齐的可靠性增强，不应被误解为 Kimi 特有语义。
