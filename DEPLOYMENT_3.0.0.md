# Model Relay 3.0.0 - Cache Execution Control 部署与迁移

## 1. 版本规则与定位

本次需求为新增“接入模型缓存机制”，属于新修改需求，因此版本从 `2.0.0` 升级为 `3.0.0`：XX 加一，YY/ZZ 清零。版本级别依据修改性质，而不是代码行数。

3.0.0 复用原有 Session / Request / Job / Material / RouteBinding 执行事实，不改变 `dispatch_started -> indeterminate` 的保守恢复边界。缓存是新的执行控制层，不是结果缓存。

## 2. 数据库升级

按顺序执行：

```text
sql/001_relay_schema.sql
sql/002_fusion_runtime.sql                  # 仅保留 legacy Fusion 时
sql/003_relay_v2_session_request_material.sql
sql/004_provider_native_file_ingress.sql
sql/005_relay_cache_control.sql             # 3.0.0 新增
```

`005` 新增：

- `relay_cache_resources`
- `relay_cache_operations`
- `relay_request_cache_bindings`
- `relay_cache_pins`
- `relay_cache_tasks`
- `relay_requests` 的 2.3 identity / ContextPlan / CachePlan / execution-fence 字段
- `accept_relay_request_v3`
- sync request fence RPC
- fenced Material/Cache Binding install
- `seal_cache_and_dispatch_v3`
- fenced `store_relay_result_v3 / complete_relay_request_v3 / fail_relay_request_v3`

在 `005` 未应用前，不允许 API 开始创建 v3 Session/Request。

## 3. 调用合同

新 Session 下 Request 只新增一个公共缓存控制：

```json
{
  "input": "Summarize the attached material.",
  "requested_cache_mode": "auto",
  "execution": {"mode": "async"}
}
```

允许值：`off / auto / on`。不开放 TTL、Provider handle、缓存 key、`cache_control` 或断点位置。

- `off`：Relay 不主动创建/引用/声明可控缓存；不承诺 Provider 自主透明缓存关闭。
- `auto`：能力/上下文/收益不合适可正常变成 None。
- `on`：有已认证能力但上下文不足 -> None 后正常推理；供应根本无已认证能力/协议未认证/安全政策禁止 -> Fail-Closed。

旧 Session 显式传 `requested_cache_mode` 会返回 `CACHE_CONTRACT_UPGRADE_REQUIRED`，应创建新 Session；旧 2.1/2.2 Request 不回填 2.3 cache 语义。

## 4. 内置 Supply 的 3.0.0 状态

- **Grok / Responses**：沿用历史 `prompt_cache_key` 行为，作为 `legacy_verified implicit_prefix`。3.0 外层 Gate 只负责 off/None 抑制 key 与统一审计，不修改 reasoning/include/store/history。
- **Gemini Native**：控制面声明 Stateful Resource 候选，但默认 `candidate`，真实 cachedContents create/get/reference/expiry/delete 联调认证前 `auto -> None`，`on -> CACHE_PROTOCOL_UNVERIFIED`。
- **Claude Messages**：Breakpoint 候选，默认 `candidate`；真实 block-level `cache_control`、门槛、TTL 与 thinking/history 回归认证前不出站缓存字段。
- **GPT / 其他 Responses**：Implicit Prefix 候选，默认 `candidate`；只有明确发布 verified Supply/Profile 后才注入认证字段。
- **Kimi / GLM / MiMo**：不因已能读取 cached token 字段而反推缓存能力。

## 5. 执行与恢复不变量

3.0 v3 Request 的发送前顺序：

```text
load frozen Request/Session
 -> freeze Material Binding
 -> prepare CacheExecutionBinding
 -> install binding with execution fence
 -> atomic seal + pin + dispatch_started
 -> exactly one model dispatch attempt
 -> archive raw/output
 -> fenced result_stored
 -> fenced Session/history commit
```

缓存资源 create/renew/delete 使用独立 operation 状态，不借用 `provider_dispatch_state`。Provider 缓存资源结果未知不等同于模型已经推理；但模型一旦 `dispatch_started`，缓存错误不能触发自动第二次生成。

sync 不创建 Job。3.0 新增 `request_executor_id/epoch/lease`，使 sync 的推理前写入、dispatch seal、result store 和 commit 也受 fencing 保护。

## 6. 灰度顺序

建议：

1. 先部署 DB + 能读 2.1/2.2/2.3 的 Worker/API。
2. 保持新增 Supply cache contract 为 candidate，先观察 ContextPlan/Resolver shadow 结果。
3. 验证 v3 fencing / 故障注入与 Grok golden wire。
4. 按 Supply 独立发布 verified cache contract。
5. 先 `auto` 小流量，再开放 `on`。

不得先让 Adapter 发送缓存字段，再补 Binding / Fencing / Lifecycle。

## 7. 验收

本归档的本地代码基线应至少通过：

```bash
python -m compileall -q app
python -c "import app.api, app.worker"
pytest -q
```

真实 Provider 缓存能力必须另做官方/聚合渠道受控联调；mock/unit test 通过不等于缓存端点已经认证。
