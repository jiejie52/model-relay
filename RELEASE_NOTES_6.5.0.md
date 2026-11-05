# Model Relay 6.5.0 缓存执行升级

本版基于上传的 6.4.2 源码。v2 Session、Material 和 Request 入口继续沿用原契约。新受理的 v3 Request 保存语义指纹、双轨候选和冻结的缓存预算。已受理请求按原快照恢复。

## 运行机制

- Relay 对结构化数据和明确无序的 Schema required 集合做规范化，保留 Prompt 字面量、消息角色和 Part 顺序。跨阶段生成的 Schema 默认名称与 Relay 附件标记改用稳定值。已经提交的历史仍直接从历史对象读取。物理链按作用域与原生请求块生成，最终 HTTP 请求体摘要另行记录。物理链里的 token 数用于保守估计。
- 同一亲和 epoch 内，AIHubMix 的缓存创建、句柄检查和推理共用会话令牌。上游明确报告断粘、未过期句柄失效或连续可比较的 warm miss 时，运行时仅在派发前窗口轮换。数据库在一次事务内检查执行权、推进 epoch 并淘汰旧资源索引。已经派发的请求只记录证据，由后续请求处理。
- 统一隐式基座负责规范化、指纹、粘性与观察。Gemini 的显式和隐式路径各自规划，最终物理发送互斥。Claude 的显式断点叠加自动前缀缓存，需要具体 Supply 和 Profile 同时启用 `composite_mode: composite_layered`，认证的 breakpoint 机制应使用 `threshold_mode: provider_enforced`。通过能力合同尚未认证的供应保留观察路径。
- 首轮可按已有认证、上下文与经济证据准备显式资源。内置策略针对符合条件的大材料开放上限为 131072 实测 token 的一次建池试用。配置价格快照或硬预算后按配置校验。滚动快照还需满足兼容前缀、几何增长和创建加存储费用的净收益门槛。
- Gemini 新执行路径按 60 MiB 原始字节预算选择 Session 材料。选择优先级依次为文本投影、小 PDF、大 PDF 或原生 DOCX、视频和其他低密度材料。选中集合沿用原有材料顺序。超预算材料继续使用既定推理绑定，同一 User Content 内的附件保留原消息边界。
- Grok 的已有 Responses 历史、reasoning、store 和缓存键实现保留。部署环境设置 `RELAY_GROK_CACHE_MODE=legacy_v1`，或在 Supply 缓存合同覆写该值，可以让新受理请求使用原有缓存键路径。环境变更通过重新加载 API 和 Worker 生效。
- HTTP 诊断默认关闭。开启时记录发送实体及响应实体的 SHA-256、实际头部和字节数。内联报文用十六进制保存原始实体。超限报文写至 `RELAY_HTTP_WIRE_LOG_DIRECTORY`，文件权限为仅所属运行用户可读写。启用诊断的环境应按原始业务数据权限管理日志与落盘目录。超限响应在流式读取期间同步落盘。

## 部署与验证

1. 对已完成既有缓存迁移的数据库执行 `sql/008_cache_v65_upgrade.sql`。全新数据库按文件编号顺序执行现有迁移。
2. 同时部署 API 和 Worker，确认两者均为 `6.5.0`。
3. 使用新 Session 验证新的冻结策略和有认证的模型供应。已有 Session 可以继续受理新请求，但已冻结供应、缓存策略和旧 Gemini 物理布局继续生效。
4. 用同一 Session 的两个真实业务请求核对 `cache_tracks`、`prefix_diff` 和 `cache_read_tokens`。按需短期开启 HTTP 诊断，核对建池与推理发送同一亲和令牌及最终 HTTP 摘要。
5. AIHubMix Router 的粘性头提供模型复用优先信号。Provider 缓存命中和计费须以真实供应的响应与账单验证。

本版包含 P0/P1 的执行路径。P3 的计费心跳调度和后台缓存清理延后。未在生产 AIHubMix 或 Supabase 环境发起调用。
