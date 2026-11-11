# Model Relay 8.8.0 五步完善说明

唯一基线为本次会话交付的 model-relay-8.7.1.zip，完整复制后修改。版本属于处理方式增强，升级 YY 位至 8.8.0。保留 requirements.txt、Dockerfile 和全部原文件，不新增 tests 文件或数据库迁移。

## 提交与恢复门禁

含新版 thinking 的请求执行通用授权核验及思考专项核验，不再仅由 Request 外壳版本决定。新版 thinking 请求在 result_stored 恢复时必须存在执行事实标记及归档的 observed_thinking；缺失时明确失败，不能走历史直接提交分支。历史无 thinking 请求仍走原兼容恢复。违约保留原始成功实体引用，禁止重发推理。

## Dify V2 模式

旧 /v1/dify/relay 不指定 execution_contract 时保持 V1。新增显式 execution_contract=v2，使用同一个入口，但内部通过原 V2 HTTP 路由复用鉴权、SessionManager、RequestAcceptor、状态查询与结果读取。兼容层不执行模型调用、路由裁决或 Fusion 业务步骤。

执行示例，HTTP 头须带 Authorization、X-Tenant-Id、X-Conversation-Hash 和稳定的 Idempotency-Key：

```json
{
  "execution_contract": "v2",
  "operation": "execute",
  "session_key": "stable-conversation-session-key",
  "session": {"provider": "gemini", "model": "gemini-3.1-flash-lite"},
  "request": {
    "input": "分析输入内容",
    "think_level": "LOW",
    "think_level_policy": "prefer",
    "execution": {"mode": "async"}
  }
}
```

复用已知 Session 时，用 session_id 替代 session 与 session_key。session_key 标识稳定会话，Idempotency-Key 标识一轮请求，二者不得在重试时更换。网关不根据正文生成新身份。创建 Session 后中断，可用原键重放两步，不产生第二次推理。

V2 返回 execution_contract、session_id、request_id、可选 job_id 和 payload。payload 保留 V2 原返回结构，业务输出仍通过 result 获取。status/result 使用 execution_contract=v2、operation、session_id 和 request_id；不把历史 V1 job_id 改造为 V2 请求。旧 V1 结果继续使用原 V1 协议。未知 request 字段明确拒绝，不能把旧 Fusion stage 等业务字段静默丢弃后提交。

## 可信审计

Dify 内部 ASGI 包装器注入可信入口上下文；普通客户端不能通过 Header 伪造入口。受理日志、request.json、数据库 metadata._relay_audit 和 execution-fact.json 记录真实入口。字段存在性以 exclude_unset 保留，区分省略、空值与显式 AUTO。审计入口不参与请求身份，恢复沿用原快照，不以恢复入口改写原始事实。

## Excel 思考单一信源

新增思考合同配置、供应思考绑定两张表。原能力合同继续管理现有非思考能力；迁移供应的思考语义只由思考映射清单与思考合同配置读取，源合同ID仅复用原非思考合同，不再从 accepted_levels 生成新供应映射。

工作簿共有 189 条映射，27 份七态规则，其中含 3 份未绑定生产供应的 none/toggle/budget 模板。18 条启用供应声明全部使用 relay-thinking/2，包括 Gemini、OpenAI、Claude、Grok、GLM、MiMo 及 Kimi；Kimi 固定开启和离散控制通过独立配置绑定，优先级也由 Excel 明确填写。新 Session 禁止引用旧格式思考合同，旧兼容解释器仅服务冻结历史。

这些是配置迁移和代码覆盖，不是实际供应认证结果。预算、开关及无控制模式模板不获得流量；固定开启不宣称满足 HIGH/MAX 硬深度要求。新增纳管供应加入既有自动部署验证门禁：缺凭据、验证失败或归档不可用即保持 pending/unavailable，不沿用旧 builtin 供应绕过检查。对现有验证器未覆盖的协议资源能力，仍返回 pending；完成该供应的独立真实验证前不能开放。API 与 Worker 应同版部署并加载一致快照。

## 本次验证

通过编译与 API/Worker 导入、18 条供应配置编译、108 个请求意图/协议投影组合、Kimi 专用规则优先级、2.2/2.4 后置违约核验与原始响应引用、执行事实缺失拒绝恢复、入口审计与身份隔离。ASGI 受控检查覆盖 Dify execute/status/result、鉴权、所有者冲突、参数省略、显式 AUTO、require、未知字段、V1 查询边界。原工作表已有值、公式及验证数量对照保留；历史请求身份计算模块未修改。模拟无认证的部署配置中，18 条声明全部为 pending，没有产生假 ready。

未运行真实账号推理、数据库 RPC、费用核对或 Railway/Docker 部署认证。不要把本地替身检查当作真实供应通过证据。原始响应、合同冻结、执行围栏、缓存内容身份和 dispatch_started 后不重发的架构边界保留。
