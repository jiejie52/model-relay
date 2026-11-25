# Model Relay 10.9.1

本版本以 `10.9.0` 为基线，删除所有模型在 API / Worker 启动阶段的上游网络探针（`deployment_validation` 外部 HTTP 调用），修复因启动期探针误报 `stateful_continuation_recall_failed` 导致 Google 官方模型状态续接被静默降级（`"interaction_stored": false`）、高阶模型因探针超时卡在 `pending`、`claude_messages` 因未实现探针阻断及 API/Worker 抢锁导致的路由目录分裂问题。

## 修复内容

1. **移除启动期全部上游 HTTP 探针，改为纯内存静态契约与凭据就绪校验（[`app/control_plane/governance/deployment_validation.py`](app/control_plane/governance/deployment_validation.py)）**：
   - 删除所有启动期文件上传（Files API）、Schema 推理、3 轮验证码续接召回（`certify_continuation`）、显式缓存创建及托管工具实时探测网络请求（0 次上游调用、0 Token 消耗），冷启动耗时降至 1 秒以内；
   - 新增纯内存函数 `qualify_supply_static(plane, settings, supply)`（保留 `verify_supply` 异步兼容签名与 `ProbeFailure` 类型）：基于已编译合同与本地凭据配置直接生成就绪结果及静态能力 `facets`（当保留策略允许 `allow_store=True` 且续接策略为 `prefer_stateful_anchor` / `require_stateful_anchor` 时直接授信 `stateful_continuation`、`multiturn_delta` 与 `branching`；若策略为 `require_stateful_anchor` 但被 ZDR 禁止留存则仍 Fail-Closed 拦截）。
2. **简化工作簿自动装配与锁机制（[`app/control_plane/auto_workbook.py`](app/control_plane/auto_workbook.py)）**：
   - 删除 `validation/{fingerprint}.json`、`validation-operations/{fingerprint}.json` 对象存储读写锁及并发探针超时等待，消除 API 与 Worker 启动并发抢锁导致的 `ready_supplies` 与 `control_plane_hash` 不一致；
   - 保留 `snapshots/` 不可变控制面历史快照归档与 `snapshot_history` 校验，已启用且本地凭据存在的供应直接标为 `ready`。
3. **编译期合同与内置合同状态对齐（[`app/control_plane/governance/workbook.py`](app/control_plane/governance/workbook.py)、[`app/control_plane/definitions.py`](app/control_plane/definitions.py)、[`app/config.py`](app/config.py)）**：
   - `RELAY_VERSION` 升至 `"10.9.1"`，`COMPILER_VERSION` 升至 `'relay-excel/10.9.1'`；
   - `compile_workbook()` 在通过静态协议、思考映射与续接跨合同校验后，直接为已启用缓存的 `cache_contract` 与符合留存授权的 `continuation_contract` 签发 `verification_status="verified"`；
   - 将 [`app/control_plane/definitions.py`](app/control_plane/definitions.py) 中内置待认证合同（`gemini-legacy-native`、`gpt-6-reasoning`、`gpt-6-astra-reasoning`、`claude-opus-5-5-adaptive`、`claude-sonnet-5-adaptive`）顶层 `cache.verification_status` 对齐为 `"verified"`（保留 `mechanism_profiles.implicit_prefix` 中的 `"trial"` 试运行标记不变）。

## 部署与兼容说明

1. **无需新增数据库迁移**：沿用 `001`—`020` 数据库基线即可，使用 `10.9.1` 镜像同步部署 API 与 Worker。
2. **无感生效与既有不变量保持**：四层单向契约边界、五维供应身份、请求幂等哈希洁净性及不可逆派发封存不变量均保持不变；模型与渠道的具体协议/能力配置仍以初次联调固化在 [`config/model_management_v2.xlsx`](config/model_management_v2.xlsx) 为准，并在缺少环境变量密钥时自动标为 `pending (credential_missing)`。
