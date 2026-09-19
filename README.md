# Model Relay 0.3 - Session / Request / Material

本版本在 `model-relay_0903` 的可靠异步 Job 基础上引入 Relay v2：

```text
Material -> Session -> Request -> sync Inline Executor
                            \-> async Job -> Worker
                                      \-> Shared Execution Runtime -> Provider Adapter
```

核心规则：

- **Session** 是 owner、材料基座、Provider connection、上下文与历史边界。
- **Request** 是每次模型调用的稳定事实身份；同步/异步请求都有 Request。
- **Job** 只负责异步调度；同步 Request 不进入 `relay_jobs`。
- **Material** 是长期事实，Provider file ID / URI / Signed URL 只是可重建 binding。
- **Error** 原始 body 完整归档并通过 `/error/raw` 交付，不再做 Provider 错误归一化/安全摘要。
- v2 Core 不解释 Dify/Fusion stage；旧 Fusion 仅存在于 compatibility 路径。
- Railway 与 Aliyun SAE 使用同一源码，通过 `execution_pool` 和 connection 配置分工。
- 当前对象存储仍为共享 Supabase，但 v2 对象全部记录 `storage_id`，为后续 R2/OSS 留出替换边界。

## 新接口

```text
POST /v2/materials
GET  /v2/materials/{material_id}
POST /v2/sessions
GET  /v2/sessions/{session_id}
POST /v2/sessions/{session_id}/requests
GET  /v2/sessions/{session_id}/requests/{request_id}
GET  /v2/sessions/{session_id}/requests/{request_id}/result
GET  /v2/sessions/{session_id}/requests/{request_id}/error/raw
POST /v2/sessions/{session_id}/requests/{request_id}/cancel
```

旧 `/v1/jobs` 保留为兼容入口。

部署、SQL 顺序和最短操作步骤见 **`QUICKSTART.md`**。

## 验证状态

本代码包已通过本地 Python 编译与单元测试；`sql/003` 尚未在你的实际 Supabase 项目执行，官方 Kimi 文件/视觉/Structured Output、SAE 到 Supabase/Kimi 的真实网络链路也需要在预发布环境做集成验收后再切生产流量。
