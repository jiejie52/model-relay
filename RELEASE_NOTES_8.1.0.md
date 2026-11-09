# Model Relay 8.1.0

## 变更

- 修正 `config/model_management_v2.xlsx` 的 Gemini 渠道顺位。
- 所有 `Google 官方（规划）` Gemini 路由统一为顺位 1。
- 所有 `AIHubMix Gemini` 和 `聚合渠道 Gemini ...` 路由统一为顺位 2。
- `gemini-3.1-pro-preview-search` 与 `gemini-3.5-flash-lite-free` 仍是聚合渠道别名，不新增或伪造 Google 官方路由。
- API 与 Worker 对外版本同步更新为 8.1.0。

## 职责边界

本次只调整控制面工作簿中的候选路由顺位，不改变执行面、运行时、协议适配器、缓存合同、材料传输、请求身份算法或恢复语义。工作簿中的渠道仍保持停用和待系统验证状态；顺位调整不等于渠道认证或生产启用。

## 部署

API 与 Worker 应同时使用 8.1.0 包。发布工作簿前仍须经过既有离线校验、认证证据审查和批准快照流程。本版没有新增 SQL 迁移或 Python 依赖。
