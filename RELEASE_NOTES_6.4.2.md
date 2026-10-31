# Model Relay 6.4.2 — 上游完整响应诊断

基于 6.4.1 完整源码。缓存执行绑定已选择机制，且 Gemini 原生响应未得到正数缓存 token 命中证据时，记录整个 AIHubMix 响应。包括原生计数缺失（unknown）、明确为零（miss）、解析失败及上游 HTTP 错误；命中和未启用缓存的请求不输出正文。缺失仍是 unknown，不能据此断言 Provider 没有缓存或费用没有节省。

`gemini_cache_unproven_upstream_response` 是清单：Relay request_id、session_id、AIHubMix 请求号（优先 `x-aihubmix-request-id`）、缓存计划与实际机制、命中状态、HTTP 状态、响应头、原始实体字节长度、SHA256、片段数量与编码。认证、Cookie、密钥等响应头只列出被隐去的名称。其后的 `gemini_cache_unproven_upstream_body_chunk` 按从零开始的 `chunk_index` 提供全部正文；按同一 `request_id` 和 SHA256 排序、拼接。`body_encoding=utf-8` 时直接拼接 `body_chunk` 文本并按 UTF-8 编码；否则每片单独 Base64 解码后按序拼接二进制。核对拼接后的字节长度与 SHA256，可发现日志平台丢行或截断。压缩实体保留原始压缩字节。原有 `gemini_cache_usage_observed` 提供解析后的 usage 诊断。

成功写入原始响应对象后，`gemini_cache_unproven_response_stored` 以相同 Relay request_id 记录 `raw_response_object_id`。即使原始响应对象存储后续失败，Adapter 的上游响应日志也已写出。AIHubMix 请求号同时进入现有 `provider_call_completed` / `provider_call_failed` 事件。

这些是诊断事件：不修改输入、CachePlan、Provider 调用、命中判定、Schema、存储合同及派发次数；日志生成异常不改变推理结果。正文可能包含用户材料或模型输出，部署平台上的日志访问与留存应按原始响应数据权限管理。片段日志量随响应大小增长。API 与 Worker 同时重建；无新 SQL、依赖或 tests 文件。未进行生产 AIHubMix 计费验证。
