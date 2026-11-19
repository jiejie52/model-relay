# Model Relay 10.1.4

## 修正目标

10.1.3 没有完整落实“由 Relay 自行包装 result”的要求：虽然移除了空壳 Schema，但仍发送 application/json 格式提示，并要求模型正文先通过 json.loads 且根类型为对象。本版明确删除这两个限制。

## 当前行为

1. Interactions 的 structured_output.mode=json_object（含 object 别名）不生成 response_format。既不发送 Schema，也不要求上游 MIME 为 application/json。未指定结构化输出的普通调用沿用原行为。
2. ResultCommitter 的该模式不解析、修复、重排、重新序列化模型正文，也不检查它是否为 JSON 或对象。模型正文原样放入 result.text，由现有 compact_result 字典和 API JSON 序列化生成返回的 result 对象。
3. 普通中文、引号、换行、代码块、数组文本、标量文本、非 JSON 文本及带空白的对象文本都可以成功包装；这些内容均不会因不是 JSON 对象而失败。JSON 序列化会安全转义字符串，字符串内部内容保持原值。
4. 新 Interactions/json_object Request 的保证标记为 relay_result_object，明确是 Relay 封装保证，不冒充上游原生 Schema 保证。
5. 非原生 Schema 供给的启动探测使用普通文本提问，不发送 response_format；探测只要求完整的非空回答，不解析模型正文或要求对象根类型。仍需认证原生 Schema 能力的供给继续用显式业务 Schema 探测，不改变该能力的验证要求。
6. Interactions 验证版本升至 deployment-probes/gemini-interactions/5，重新验证对应供给。

返回示例：模型回答“你好，我是助手。”时，Relay 返回的 result 是 {"text":"你好，我是助手。", ...}。result.text 不再被要求先是 JSON 对象字符串，也不会强行改成 {"answer":...} 等业务结构。

显式 structured_output.mode=json_schema 仍代表调用方主动要求的业务结构，真实 Schema 的下发与校验保留。其他授权、模型身份、思考合同及原始协议状态检查也保留；本次删除的是业务正文 JSON 对象限制，不是把所有错误改成成功。

## 部署与验证

API、Worker 一起升级到 10.1.4，无新依赖或 SQL 迁移。用原有 Postman 的 02 新建 Session 后执行 03-1；无需修改原来的 json_object 参数。对照日志确认该请求不含 response_format。

原始响应、Usage、thought/signature、历史内容和单次派发约束保持不变。没有修改提示词、温度、思考档位、输出预算、路由优先级或回退门禁。旧失败请求不会被自动重发。

从用户原始只读 model-relay-10.0.4.rar 全新完整解压并保留全部 389 个文件，再重放已交付补丁。保留隐藏文件，不初始化 Git，不新增 tests 文件。

现有 68 项 unittest 和 3 项函数检查通过。定向验证实际 ResultCommitter 对普通中文、数组/数字/非 JSON/代码块等正文均成功提交并原样包装；另外通过禁用 JSON 解析器验证 json_object 不依赖 json.loads。显式业务 Schema 的非法输出仍拒绝。6 个 Google 供给通过严格模拟上游探测；未使用真实 Google 凭据，部署后的生成表现仍待实际复测。
