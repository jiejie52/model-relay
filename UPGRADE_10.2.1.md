# Model Relay 10.2.1

Gemini Interactions 的续接以已提交的原生历史为准。兼容的父锚点覆盖全部历史时，Relay 给 Google 发送上一轮 Interaction ID 和本轮输入。系统指令、工具声明和生成配置按本轮请求投影。因此，在同一会话更新这些参数时，Relay 可以继续引用包含相同历史的父锚点。会话、租户、上游账号与路由、已提交步骤及材料绑定仍参与续接校验。[Google Interactions API 文档](https://ai.google.dev/gemini-api/docs/interactions-overview#server-side-state)

## 两轮请求

创建使用官方 Google Interactions 路由的累计 Session，设置 `context_policy` 为 `conversation`。在同一 Session 下通过 `POST /v2/sessions/{session_id}/requests` 顺序提交两轮。每轮使用自己的幂等键，并在上一轮成功提交后再发下一轮。

首轮的 `input` 由长参考资料和问题 1 组成。示例中的资料占位符可替换为实际材料文本。用 `instructions` 指定本轮系统规则。

```json
{
  "input": "【长参考资料】\n\n问题 1：总结资料的主要内容。",
  "instructions": "根据会话中的参考资料简洁回答。",
  "execution": {"mode": "sync"},
  "requested_cache_mode": "auto"
}
```

第二轮的 `input` 只包含新问题。如果本轮仍需系统规则，可以继续发送同样的规则或更新规则。Relay 从已提交的锚点生成上游 `previous_interaction_id`。

```json
{
  "input": "问题 2：资料中的适用条件是什么？",
  "instructions": "根据会话中的参考资料简洁回答，并引用章节。",
  "execution": {"mode": "sync"},
  "requested_cache_mode": "auto"
}
```

用上游请求的 `previous_interaction_id`、`input` 和 `body_bytes` 核对实际续接和传输体积。用 Google 返回的 `usage.total_input_tokens` 与 `usage.total_cached_tokens` 核对输入与缓存计数。历史引用可以省去重复传输，Google 仍按实际使用量报告缓存命中。[Google 隐式缓存文档](https://ai.google.dev/gemini-api/docs/caching)

## 升级

1. 从 10.2.0 同步升级 API 和 Worker，继续使用已安装的 SQL 016。
2. 升级前封存的请求按原指纹校验。已有会话第一次按新指纹续接时，Relay 会重放可见历史来建立新锚点。后续有合格锚点时恢复增量发送。
3. `explicit` 和 `session_materials_only` 仍按各自的可见范围发起无状态调用。首轮长资料应作为 `conversation` 的普通输入提交，既有的系统指令按原字段发送。
