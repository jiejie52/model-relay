# Kimi Provider 参考（2026-09-17）

本页只记录本实现直接依赖的 Provider 协议事实；业务语义仍由调用方负责。

## 默认接入

国际站默认 Endpoint Profile：

```text
MOONSHOT_BASE_URL=https://api.moonshot.ai/v1
POST /chat/completions
Authorization: Bearer <MOONSHOT_API_KEY>
```

中国区账号/Key 与国际区可能是隔离的；如果部署使用中国区账号，请通过环境变量改为对应官方 Endpoint，不要把 base URL 放到客户端请求中。

## K3

本包把 `kimi-k3*` 映射到能力快照：

```text
moonshot-k3-chat/2026-09-17
```

`generation.reasoning.effort` 允许 `low | high | max`，Adapter 映射到顶层 `reasoning_effort`。K3 多轮历史保存完整 assistant message，而不是只保存可见 `content`。

## K2.6 / 其他 Moonshot Chat 模型

`kimi-k2.6*` 使用：

```text
moonshot-k2.6-chat/2026-09-17
```

其思考控制由 `generation.thinking` 进入 Provider Adapter。其他模型使用 `moonshot-chat-generic/1`，不对未核验的模型能力做静默推断；Provider 拒绝的参数会通过 Raw Error Contract 原样返回。

## Structured Output

Relay 的 canonical `structured_output` 在 Moonshot Chat Adapter 中投影为 `response_format`，并在响应后使用调用方原 schema 再做通用校验。Adapter 不解释 schema 内的业务字段。

## Prompt cache

同一个 Relay Session 生成稳定 `prompt_cache_key`，连续请求保持不变，用作上游缓存路由提示。

## 材料输入

本包当前只自动处理：

- 文本；
- `data:` Base64 图片/视频；
- 已经准备好的 `ms://` 媒体引用。

通用远程 URL、legacy `input_image/input_file` 不会被偷偷转换或降级，当前会 Fail-Closed。需要生产级文件上传/抽取时，请增加独立 Material Preparation 流程并做合同测试。

## 官方文档入口

```text
https://platform.kimi.ai/docs/api/chat
https://www.kimi.ai/help/kimi-api/api-model-selection
https://www.kimi.ai/help/kimi-api/api-troubleshooting
```

实际开放模型以部署账户通过官方模型列表/API 查询到的结果为准。
