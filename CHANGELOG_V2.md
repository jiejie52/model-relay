# Relay v2 改造摘要

## 新增

- `/v2/materials`：Material Ingress，临时源立即读入共享 Object Storage，记录 SHA-256/size/storage_id。
- `/v2/sessions`：统一 Session，固定 provider/connection/model/context policy/execution pool。
- `/v2/sessions/{id}/requests`：统一 Request；支持 `sync`/`async`，只有 async 创建 Job。
- `relay_requests`：新的调用事实表；`relay_jobs` 降为异步调度表。
- `relay_objects`：所有 v2 大对象的存储定位事实，显式保存 `storage_id`。
- `relay_materials` 与 `provider_material_bindings`：稳定材料身份 + 可重建 Provider binding。
- 官方 Kimi/Moonshot Chat Adapter，文件提取、Base64 图片、Provider file binding 重建。
- 原始 Error 通道：完整 body + hash + HTTP 元数据，新增 `/error/raw`。
- Worker `execution_pool` 过滤、`lease_epoch` fencing、`result_stored` 后恢复提交、Provider dispatch 后失租转 `indeterminate`。
- sync Request deadline；超时/进程丢失不自动转 async 重放。
- ObjectStorage/StorageRegistry 接口，为 R2/OSS 后端预留。
- Worker SIGTERM drain。

## 保留

- 原 Postgres durable Job queue、`FOR UPDATE SKIP LOCKED`、Lease、Heartbeat。
- `/v1/jobs`、旧 job_id 查询/取消与旧 Fusion 兼容执行。
- Grok `response.output`/encrypted reasoning 外部历史。
- Structured Output provider projection + canonical JSON Schema 校验。
- Supabase Postgres/Storage 首期继续作为 Railway/SAE 共享控制面和对象存储。

## 明确改变

- 新链路不再以 Job 作为所有调用的身份；Request 才是事实权威。
- 新链路不再把 Dify/Fusion stage 作为 Relay Core 的执行分支。
- Provider Error 不再映射为 `UPSTREAM_BAD_REQUEST/UPSTREAM_SERVER_ERROR`，不做摘要/截断。
- Signed URL / Provider file id 不再作为材料长期事实。
