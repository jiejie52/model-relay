# Model Relay 0.4.0 - Provider-native File Ingress

本版本以 `model-relay-v2_0.3.1` 为基线，实现文件上传机制增强补丁 V1.1：

```text
Caller file / readable URL
        |
        v
Relay Material Registry (stable material_id)
        |
        +--> AIHubMix Gemini Native Proxy -> Gemini Files API -> fileUri
        |
        +--> Official Kimi Files API
        |       +-- text: file-extract -> /content -> Provider-derived artifact
        |       +-- image/video: ms://<file_id>
        |
        `--> Supabase input-file fallback (conditional only)
```

## 0.4.0 核心变化

- **Gemini native-first**：`aihubmix_gemini_native` 连接直接使用 AIHubMix Gemini Native Proxy 的 resumable Files API。成功后记录 `file.name/file.uri` binding，原始输入文件默认不写 Supabase。
- **Kimi official Files API**：`moonshot_official` 连接直接调用官方 `/v1/files`。文本类走 `purpose=file-extract` 并读取 `/content`；图片/视频分别使用 `purpose=image/video` 与 `ms://<file_id>`。
- **Supabase 收窄为输入文件 fallback/bridge/durability**：只有 Provider 暂时不可用、Provider 需要 URL bridge、或显式 `relay_backed` 策略时保存原始输入字节。
- **Relay Artifact Storage 不变**：request snapshot、raw response/raw error、Session history、Kimi provider-derived extraction、Fusion artifact 等仍按现有方式使用 Supabase；本补丁只改变“输入文件原始字节”的默认落点。
- **稳定 Material 身份**：`material_id` 与 Provider file ID/URI 解耦；binding 按 `connection_id + account_scope_hash + generation` 隔离。
- **Request 冻结 binding generation**：第一次 Provider dispatch 前写入 `relay_requests.material_binding_snapshot`，重试/恢复不会静默换文件 binding。
- **Raw Error 规则继续生效**：Gemini/Kimi Files API 错误不截断、不映射为 `UPSTREAM_*`，完整 body/headers/phase 可返回并归档。
- 原有 Session / Request / optional Job、Lease/Heartbeat/Fencing、Checkpoint + 幂等、Structured Output 与 `/v1/jobs` 兼容路径继续保留。

## 新增文件

- `app/materials/provider_files/gemini_aihubmix.py`
- `app/materials/provider_files/kimi_official.py`
- `app/materials/provider_files/registry.py`
- `app/materials/binding_resolver.py`
- `app/materials/fallback_storage.py`
- `app/providers/gemini_native.py`
- `sql/004_provider_native_file_ingress.sql`

部署与最短测试步骤见 `QUICKSTART.md`。

## 验证状态

本代码包已通过 Python `compileall`、API/Worker import smoke 与 38 项单元测试。单元测试覆盖 native 成功不写 input fallback、Provider 暂时失败 fallback、关闭 fallback 时失败、429 原始错误不隐式落盘、account scope 隔离、Kimi file-extract/image/video wire 语义，以及 0.3.1 的原始 Error 回归测试。

真实上线前仍必须使用你的 AIHubMix Gemini Native Proxy endpoint、Kimi 官方 Key、Supabase 与目标模型做预发布集成验收；本代码包不会伪造“已对你的外部账号完成联调”。
