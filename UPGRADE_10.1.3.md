# Model Relay 10.1.3

## 本次行为

针对 Interactions 的 json_object 请求，移除 Relay 额外构造的空壳 Schema。发送格式仅为：

```json
{"response_format":{"type":"text","mime_type":"application/json"}}
```

如果调用方使用 json_schema 并提供真实业务 Schema，则继续将该 Schema 原样深拷贝放入 response_format.schema，后置校验原始业务结构。不删除真实 Schema，也不替调用方编造 answer 等业务字段。

## Relay 结果包装

对 Interactions/json_object，Relay 在上游原始响应已归档后解析返回文本，确认它是 JSON 对象，再以紧凑 JSON 重新序列化，放入现有 result.text。外层 result 继续由 Relay 组织为 JSON 对象，text 保持字符串类型，兼容现有调用方。仅删除 JSON 结构中的无意义空白，字符串值内部的空格保持原样。

例如上游返回 `{ "name": "a  b" }`，result.text 为 `{"name":"a  b"}`。原始返回、原生历史、thought/signature 和上游报告的 Usage 保持原样。规范化展示不改变已经发生的 Token 消耗，也不会把空对象解释为满足业务任务。

json_object 只保证对象形态；合法的空对象仍为合法对象。非 JSON、数组或标量不能冒充业务 JSON 对象成功返回。非标准 NaN/Infinity 值不能序列化为合法结果。原始证据仍保留，失败不自动重发。

## 证据与探测

新建 Interactions/json_object Request 的 structured_output_guarantee 明确为 json_object_post_validate。即使该模型另外支持原生 Schema，也不会给本次没有业务 Schema 的请求冒认原生 Schema 强约束。真实 json_schema 的冻结保证与原生证据校验保持原行为。旧请求的冻结合同不被回写或静默降级。

探测与实际调用使用同一格式投影器。启动探测除检查 JSON 对象外，还必须收到明确要求的 ok=true；空对象和 ok=false 不再通过探测。Interactions 验证版本升至 deployment-probes/gemini-interactions/4，重新验证相应供给。

## 部署

API 和 Worker 一起升级到 10.1.3，无新依赖或 SQL 迁移。检查启动后目标供给是否 ready，再使用现有 Postman 的 02 创建新 Session、03-1 发送请求。调用方继续使用 structured_output.mode=json_object。temperature、think_level、输出预算均按调用方原设置处理，本版不擅自增设上限或修改提示词。

思考深度作为当轮 generation_config 控制参数继续沿用；父链、Session 隔离、优先级与拒绝回退策略、单次派发和 result_stored 仅恢复提交的架构约定均保留。

## 基线与验证

从用户上传的只读原始 model-relay-10.0.4.rar 全新完整解压，保留全部 389 个原始文件及隐藏文件，再重放已交付补丁。无新增 tests 文件。

现有 68 项 unittest、3 项函数检查通过；另以严格模拟上游验证 6 个 Google 供给，并验证无 Schema JSON 格式、真实 Schema 保留、实际 ResultCommitter 的 JSON 规范化、字符串内部空白保留、原始历史和签名不变、Usage 不变及非法结果拒绝。没有使用真实 Google 凭据，部署后的真实生成质量与 Token 消耗仍需实测。

官方定义：https://ai.google.dev/api/interactions-api 。TextResponseFormat.schema 为可选字段；mime_type=application/json 可以单独声明 JSON 输出格式。
