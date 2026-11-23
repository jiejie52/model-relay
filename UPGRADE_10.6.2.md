# Model Relay 10.6.2

## 修复根因

在已完成 `sql/018_mutable_container_taint_closure.sql` 的 Supabase 数据库上，若早先安装的是 `anchor_class` 加入之前的早期版 `016`（例如 `10.2.0` 升级后仅增量执行了 `017` 与 `018`），由于 `017` 与 `018` 未包含对 `016` 续接表字段的幂等 `ADD COLUMN IF NOT EXISTS` 兜底，而 `018` 新增触发器 `retire_linear_anchors_on_history_advance_v92` 会在每次 `relay_sessions.history_version` 递增时扫描 `public.relay_continuation_anchors.anchor_class`，从而导致请求在提交阶段（如 `complete_relay_request_v66`）触发 PostgreSQL `42703: column "anchor_class" does not exist` 错误。

## 修复内容

1. **新增前向幂等修复迁移 [`sql/019_continuation_schema_repair.sql`](sql/019_continuation_schema_repair.sql)**：
   - 幂等补齐 `public.relay_continuation_anchors`、`public.relay_anchor_pins`、`public.relay_requests` 在 `016` 与 `017` 中的全部字段、状态约束、索引与权限，同时确保相关触发器及函数处于 `016`/`017`/`018` 最新版本而不降级既有 `018` 行为，并通知 PostgREST 重载 Schema 缓存。
2. **新增运行时续接 Schema 兼容性前置核验（[`app/v2_repository.py`](app/v2_repository.py)、[`app/execution_plane/request_acceptor.py`](app/execution_plane/request_acceptor.py)、[`app/api.py`](app/api.py)、[`app/execution_plane/schedulers/worker_daemon.py`](app/execution_plane/schedulers/worker_daemon.py)）**：
   - 仅在检测到缺失 `016`/`017` 续接列/表时记录 `continuation_schema_incompatible` 错误日志（健康时静默并缓存在内存中，不增加日常请求开销）；
   - 若数据库尚未执行修复 SQL，新请求在接入阶段即返回 `503 CONTINUATION_SCHEMA_NOT_INSTALLED`，避免在必然提交失败前盲目发起上游模型调用浪费额度；SQL 修复完成后下一请求会自动复核通过，无需重启服务。

## 部署步骤

1. 在 Supabase SQL Editor 中执行 [`sql/019_continuation_schema_repair.sql`](sql/019_continuation_schema_repair.sql)。
2. 同步部署 `10.6.2` API 与 Worker 进程（支持先跑 SQL 或先发版后跑 SQL，执行 `019` 后自动自愈生效）。
