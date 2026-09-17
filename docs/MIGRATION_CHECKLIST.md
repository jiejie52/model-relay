# 迁移与回滚检查表

## 上线前

- [ ] 备份 Supabase/Postgres。
- [ ] 确认生产库已经有 `001_relay_schema.sql`；使用 Fusion 的环境确认 `002_fusion_runtime.sql`。
- [ ] 执行 `003_core_v2_kimi.sql`。
- [ ] 配置 `MOONSHOT_API_KEY`，不要把 Key 放进客户端请求或 Dify DSL。
- [ ] 运行 `bash scripts/check.sh`。
- [ ] 用测试 tenant 运行 `scripts/smoke_kimi.py`。
- [ ] 确认 `/health` 返回 `0.3.0-session-core-kimi`。

## 滚动升级

- [ ] 先升级数据库领取函数，再产生 `core-v2` Job。
- [ ] 启动新版 `app.worker`，确认只领取 `core-v2/core-legacy-v1`。
- [ ] 若仍存在 Fusion 兼容 Job，保留 `app.application.fusion_worker`，其领取范围仅为 `fusion-legacy-v1`。
- [ ] 先对新会话开放 `/v2`；不要把一个旧普通多轮 Session 自动切到 Moonshot/Kimi。
- [ ] 旧 `job_id`、Checkpoint 和结果查询路径继续可读。

## 必测故障窗口

- [ ] 相同 owner + idempotency key + 相同 fingerprint 返回原 Job。
- [ ] 同 key 不同请求返回冲突，不复用错误 Job。
- [ ] 已得到 `job_id` 后恢复网络轨迹只有 status/result，没有 execute。
- [ ] Session append 并发时第二个不同 Job 被拒绝/阻止，不产生双份历史费用窗口。
- [ ] 杀掉 Worker，Lease 到期后可被重领；旧 fencing generation 不能晚提交。
- [ ] Provider 429/4xx/5xx 的状态、Headers、Body 与上游接收到的原文一致。
- [ ] 大 Provider Error 可以从 `/error/raw` 完整读取并校验 sha256。
- [ ] 跨 tenant/conversation 读取 job/session 返回不可见。

## 回滚

- [ ] 停止新的 `/v2` 流量，不删除 `003` 新字段/函数。
- [ ] 已创建的 `core-v2` Job/Session 继续由对应版本服务完成/读取。
- [ ] 不通过重新 execute、切换 Provider、重建 Session 来“回滚”在途任务。
- [ ] 不把 Raw Error Contract 回滚成安全摘要，否则新旧客户端会出现语义分裂。

## 数据兼容说明

旧错误如果历史上已经被截断且没有完整对象归档，升级后无法恢复原始字节，应如实标记历史数据不完整；不要伪造完整 Raw Error。
