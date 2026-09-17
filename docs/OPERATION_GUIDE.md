# 简短操作指示

## 1. 数据库升级

**已有环境先备份数据库。**

新装按顺序执行：

```text
sql/001_relay_schema.sql
sql/002_fusion_runtime.sql          # 只有仍使用 Fusion 兼容路径才需要
sql/003_core_v2_kimi.sql
```

已有 0.2.x 环境通常只需要执行 `003_core_v2_kimi.sql`，但前提是 `001/002` 已经存在。

`003` 会保留原 Job ID，并给历史 Job 写入 `execution_engine`，使 Core Worker 与 legacy Fusion Worker 分开领取任务。

## 2. 配置环境变量

复制：

```bash
cp .env.example .env
```

至少配置：

```text
RELAY_API_TOKEN
SUPABASE_URL
SUPABASE_SECRET_KEY
SUPABASE_BUCKET
MOONSHOT_API_KEY          # 使用官方 Kimi 时
```

仅 Kimi 部署可以不设置 `AIHUBMIX_API_KEY`。

## 3. 启动

API：

```bash
uvicorn app.api:app --host 0.0.0.0 --port 8000
```

Core Worker：

```bash
python -m app.worker
```

如果队列中仍有 `fusion-legacy-v1` Job 或仍需旧 Fusion 入口，再启动：

```bash
python -m app.application.fusion_worker
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

## 4. 自检

不调用真实模型：

```bash
bash scripts/check.sh
```

真实 Kimi Smoke Test：

```bash
export RELAY_BASE_URL=http://127.0.0.1:8000
export RELAY_API_TOKEN='...'
export TENANT_ID='smoke-tenant'
export CONVERSATION_HASH='smoke-conversation'
export KIMI_MODEL='kimi-k3'   # 以当前账户实际开放模型为准
python scripts/smoke_kimi.py
```

脚本会：创建 Session → 提交一个 Job → 只用 result/status 轮询直到终态。拿到 `job_id` 后不会再次 execute。

## 5. 生产切换建议

先运行 DB migration，再升级 API，随后启动新版 Core Worker；确认新版 Worker 正常后再开放 `/v2` 新 Session 流量。旧 Job 不要重建或重发，通过旧 `job_id` 继续查询/恢复。

如需回滚，只停止**新的** `/v2` 流量；已经创建的 `core-v2` Job/Session 仍应由对应版本继续完成和读取，不要通过换 Provider 或重新 execute 的方式回滚。

## 6. 原始错误读取

Provider 失败时，`GET .../result` 的 Envelope 会保留 Provider 原始 HTTP 状态及可内联的原文。大 Body 通过：

```text
GET /v2/sessions/{session_id}/jobs/{job_id}/error/raw
```

读取。该接口仍需要 Bearer Token、`X-Tenant-Id` 和 `X-Conversation-Hash`。
