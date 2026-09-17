# Model Relay 0.3 — Session Core + Official Kimi Adapter

本包以现有 Model Relay 为基线完成一次兼容式改造：保留异步 Job、Postgres 队列、Lease/Heartbeat、Storage 大对象外置，以及 **Checkpoint 主恢复 + Idempotency 兜底**；新增统一 `/v2` Session API、官方 Moonshot/Kimi Provider Adapter、原始 Provider Error 合同，并将旧 `/v1/dify/relay` 降为 Compatibility Adapter。

## 架构边界

```text
Application / Dify / Fusion Runtime
               |
        Compatibility Adapter
               |
          Relay Core v2
 Session / Job / Queue / Recovery / Storage refs
          /             \
 Provider Adapters      Supabase
  - Moonshot/Kimi
  - OpenAI-compatible
```

Relay Core 不解释 Candidate、Conflict、Quality、Final Draft 等业务含义。`stage/label/metadata` 只能作为不透明追踪数据。Fusion 兼容执行位于 `app/application/`，Core Worker 不导入它。

## 本次关键变化

- 新增统一 Session API：`/v2/sessions/...`，所有 JSON 控制面返回 `relay-envelope/2.0`。
- 普通多轮 Session 固定 `provider + upstream_profile + protocol + history_codec`；不能自动跨厂商或协议漂移。
- 新增官方 `moonshot` Provider，Kimi 使用独立凭据/Endpoint Profile，不复用 AIHubMix Key。
- Provider HTTP Error 保留原始 HTTP 状态、响应头、完整 Body；不再映射成统一上游错误码，也不做安全摘要。
- Kimi 多轮历史由 `moonshot-chat/1` Codec 保存完整 assistant message，Core 只管理历史对象引用和版本。
- 新增 request fingerprint、Session 单 append in-flight、Lease fencing、Session 历史与 Job success 原子闭合。
- 旧 `/v1/jobs`、`/v1/dify/relay`、旧 `job_id` 和恢复语义继续兼容。
- 新/旧 Worker 由 `execution_engine` 隔离，避免滚动升级时旧 Worker 领取新协议任务。

## 目录重点

```text
app/
  api.py                       # composition root
  core_api.py                  # /v2 Session API
  core_service.py              # provider-neutral Core
  core_models.py               # relay-envelope/2.0
  error_contract.py            # raw error contract
  worker.py                    # Core Worker only
  providers/
    moonshot.py                # official Kimi/Moonshot Chat Completions adapter
    openai_compatible.py       # existing Responses-compatible adapter
    registry.py
  compat/
    v1_jobs.py                 # /v1/jobs compatibility
    dify_gateway.py            # /v1/dify/relay compatibility
  application/
    fusion_runtime.py          # legacy Fusion application runtime, outside Core
    fusion_worker.py           # optional compatibility worker
sql/
  001_relay_schema.sql
  002_fusion_runtime.sql
  003_core_v2_kimi.sql         # v2 migration
examples/
scripts/
docs/
```

## 快速开始

1. 已有部署先备份数据库，然后按顺序确认 `001`、`002` 已执行，再执行 `sql/003_core_v2_kimi.sql`。
2. 复制 `.env.example` 为 `.env`，至少填写 Relay Token、Supabase 凭据；使用 Kimi 时填写 `MOONSHOT_API_KEY`。
3. 安装并启动：

```bash
python -m pip install -r requirements.txt
uvicorn app.api:app --host 0.0.0.0 --port 8000
```

另开一个进程：

```bash
python -m app.worker
```

若仍需处理升级前/旧入口的 Fusion Job，再开：

```bash
python -m app.application.fusion_worker
```

4. 验证：

```bash
bash scripts/check.sh
python scripts/smoke_kimi.py
```

完整部署、API 示例、迁移和回滚说明见 `docs/OPERATION_GUIDE.md`、`docs/API_V2.md`、`docs/MIGRATION_CHECKLIST.md`。

## Kimi 材料输入边界

首版 Adapter 对文本、`data:` 媒体以及已经准备好的 `ms://` 引用做明确传输；**没有在 Core 内静默实现“任意远程 URL / 旧 input_file → Kimi 文档抽取”的转换**。普通远程媒体 URL 和未准备的 legacy `input_image/input_file` 会 Fail-Closed，避免把材料降级后仍声称完整处理。需要文件抽取/上传时，应在 Provider 侧增加明确的 Material Preparation 实现后再开放相应能力。

## 恢复不变量

- 有 `job_id`：只走 status/result，不能重新 execute。
- 只有 PREPARED + 原稳定 idempotency key：才重放原创建请求并附着既有逻辑 Job。
- 业务 Artifact/状态真正落盘后，调用方才能清理业务 Checkpoint。
- 本实现保证逻辑 Job 去重和可恢复，不承诺上游严格 exactly-once。
