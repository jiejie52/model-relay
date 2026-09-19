# Model Relay 0.3.0 Patch Notes

本版本是基于 `model-relay_0903` 的 Session / Request / Material 改造版。

主要变化：

- 新增 `/v2/materials`、`/v2/sessions`、`/v2/sessions/{session_id}/requests`。
- Session 作为统一上下文边界；Request 作为同步/异步调用的事实身份；Job 仅承担异步调度。
- 新增 Material/Object/Provider Binding 数据模型，当前继续使用共享 Supabase Storage，并通过 `storage_id` 为 R2/OSS 预留后端边界。
- 新增官方 Moonshot/Kimi Chat Adapter；Provider 协议差异仅存在于 Adapter。
- Error 通道改为保存并交付原始错误 body，不再做 `UPSTREAM_*` 归类、摘要或截断。
- Worker 增加 `execution_pool`、`lease_epoch` fencing 和 `indeterminate` 恢复边界；Railway 与 Aliyun SAE 使用同一源码、不同配置。
- 旧 `/v1/jobs`、旧 job_id 与旧 Fusion 执行路径继续保留在 compatibility 层。

数据库需新增执行：`sql/003_relay_v2_session_request_material.sql`。

部署步骤、环境变量及最短调用流程见 `QUICKSTART.md`；详细改造摘要见 `CHANGELOG_V2.md`。
