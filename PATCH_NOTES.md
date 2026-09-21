# Model Relay 0.4.0 Patch Notes

基线：`model-relay-v2_0.3.1`  
设计依据：`Model_Relay_改造方案_文件上传机制增强补丁_V1.1_20260921`

## 文件入站改造

- 新增 `ProviderFileAdapter` 与 connection-scoped registry。
- Gemini：AIHubMix Gemini Native Proxy -> resumable Gemini Files API -> ACTIVE `fileUri`；默认不写 Supabase 原始输入文件。
- Kimi：官方 Files API；文本 `file-extract -> /content`，图片/视频 `image/video -> ms://<file_id>`。
- Material Registry 与输入 payload 存储解耦；`material_id` 继续作为调用方稳定 ID。
- Supabase 原始输入文件存储改为 `FallbackObjectStorage`，只由 fallback/bridge/durability 策略触发。
- Kimi 抽取文本作为 Provider-derived artifact 保存，不等价于保存原始输入文件。

## 恢复与隔离

- `provider_material_bindings` 增加 `account_scope_hash`、`generation` 与 provider resource 字段。
- Request 首次 dispatch 前冻结 `material_binding_snapshot`。
- Key/account scope 变化不会复用旧 Provider file ID/URI；没有 fallback 时返回 `MATERIAL_REUPLOAD_REQUIRED`。
- provider upload attempt 增加独立审计记录，保留 phase/request-id/raw response/raw error/uncertain 状态。

## Error

- 继续沿用 0.3.1 原始 Error 规则。
- Gemini/Kimi Files API 非 2xx body、headers、request ID 与上传阶段完整记录；不使用 `UPSTREAM_*` 归一化，不复制 DSL 的 2K/4K 截断。
- 默认 `on_provider_unavailable` 只对连接/超时/408/425/5xx/processing-timeout 进入 fallback；429/业务 4xx 原样失败，避免隐式改变文件留存策略。

## 数据库

从 0.3.1 升级只需要执行：

```text
sql/004_provider_native_file_ingress.sql
```
