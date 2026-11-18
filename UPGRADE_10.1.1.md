# Model Relay 10.1.1

## 修复原因与兼容方式

用户的单因素对照已将故障定位到启用 `structured_output.mode=json_object` 的路径。10.1.0 将该意图投影成 `response_format={"type":"object"}`，没有按官方的 TextResponseFormat 封装；JSON Schema 也直接放在 response_format 顶层。这两处现已修复。

调用方接口保持原样，继续发送：

```json
"structured_output": {"mode": "json_object"}
```

Relay 实际发往 Google 的格式改为：

```json
"response_format": {
  "type": "text",
  "mime_type": "application/json",
  "schema": {"type": "object"}
}
```

对于 `mode=json_schema`，完整的调用方 JSON Schema 深拷贝到同一封装的 `schema` 字段，保留 properties、additionalProperties、nullable 类型数组、$defs/$ref、数组等语义；不再套用 generateContent 的旧 responseSchema 投影。原始 Schema 仍用于 Relay 提交前校验。没有请求结构化输出时，继续不发送 response_format。

温度、模型 ID、输入、指令、缓存和 thinking 映射均不因本修复自动改变。

## 关联修复

- 实际调用与部署探针共用同一个响应格式投影函数，避免“测试一种格式、上线另一种格式”。
- Interactions 的非原生 Schema 探针现在实际验证 JSON 对象模式，而不只请求普通文本；原生 Schema 探针使用官方完整封装。探针要求 completed，并校验正文为 JSON 对象或符合声明的 Schema。
- 移除 Interactions 探针收到 400/404/405 后转测 generateContent、仍将 Interactions 标记 ready 的行为。失败返回真实验证状态，不自动切换协议。
- 仅升级 Interactions 的验证版本标识，现有签名验证缓存中的对应旧 ready 记录不会复用；其他协议的验证版本保持原值。
- 运行时与探针共用正文提取函数，支持 `steps[].model_output.content[]`，保留旧响应兼容解析。
- Interactions 的 json_object 结果提交前必须能解析为 JSON 对象；数组、标量和非法 JSON 不能提交为成功。校验失败仍保留已经归档的上游原文引用。
- 补齐对外 RawErrorMeta 的 body_source、body_complete、response_diagnostics、reason_code、interaction_status 字段，避免 10.1.0 记录了诊断、API 序列化却将其过滤。
- 保留 10.1.0 的原始响应解析前归档、部分正文保存、取消/超时阶段诊断、Usage 零值和 thought signature 保留。

## 部署与复测

1. API 与 Worker 一并升级至本完整源码包，运行版本应显示 10.1.1。无需新增依赖、数据库列或 SQL 迁移。
2. 启动时会重新验证 Interactions 的官方 JSON 路径。查看配置状态及验证结果；400、404 或 Schema 验证失败不会再通过其他协议误认证。非 2xx 探针响应的脱敏正文会进入 deployment_probe_http_error 日志。
3. 使用用户原先已跑通的通用用例，保留成功时的其他参数，仅增加 `structured_output={"mode":"json_object"}`，新建 Session、使用新幂等键复测。随后再复测原始 task_decomposition。无需修改调用方字段为 Google 原生字段。
4. Wire 日志的 response_format 结构应包含 type、mime_type、schema。若需要分析失败，保留 Request ID、API/Worker 日志和 error.response_diagnostics。

Session 隔离、冻结合同、原子发送封存、单次派发、120 秒默认同步截止和 result_stored 仅恢复提交继续沿用。旧 indeterminate 请求不会自动重发或解锁。归档响应证据不等于发布 result_stored，也不授权重新推理。

## 基线与验证边界

原始 `model-relay-10.0.4.rar` 保持只读，本交付从其全新完整副本构建，并重放已完成的 10.1.0 响应记录补丁。本次修改按 Bug 修复定为 10.1.1。保留原包全部 389 个文件及隐藏文件，不初始化或重建 Git。

现有 68 项 unittest 与 3 项函数检查通过；其中既有 Interactions 探针用例已改用官方 content 数组，并将“404 回退后 ready”的错误期望纠正为拒绝。未新增测试文件。另完成 65 项离线定向断言，直接检查序列化 HTTP 请求中的官方封装、json_object/Schema/无格式路径、原始 Schema 保留、冻结输入不变、探针与运行时一致、拒绝协议降级、诊断字段返回及非法对象提交拦截。

尚未部署到用户环境，也未以真实 Google 凭据调用上游。离线验证不能替代部署后的真实请求复测。

官方依据：[结构化输出示例及 JSON Schema 支持](https://aistudio.google.com/docs/structured-output)、[Interactions 响应格式迁移说明](https://aistudio.google.com/docs/interactions-breaking-changes-may-2026)、[TextResponseFormat API 定义](https://aistudio.google.com/api-reference/interactions-api#Resource:ResponseFormat)。
