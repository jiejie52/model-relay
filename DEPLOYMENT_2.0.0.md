# Model Relay 2.0.0 — 多模型渠道治理升级

## 1. 版本定位

2.0.0 在 1.0.0 的 Session / Request / Material / RouteBinding 执行与恢复机制之上，增加“模型供应治理控制面”。本版本不改变以下恢复不变量：

- Request 仍是一次调用与恢复的稳定事实身份；Job 仍只用于 async 调度。
- Session 创建时冻结执行路由；已存在 Session 不因后续配置发布静默换渠道。
- `provider_dispatch_state=dispatch_started` 后出现未知结果仍进入 `indeterminate`，不会因为存在备用渠道而自动重发。
- Provider Adapter 仍独占 Native Wire；业务调用方只表达 canonical 调用意图。
- Canonical Material 与 Provider Binding 继续分离。

无新增 SQL migration。

## 2. 新的运行时对象

2.0.0 引入 `ModelControlPlane`，把模型、渠道/账号、协议、能力合同、模型供应关系与优先级分开管理。

```text
Published ModelControlPlane Snapshot
  ├─ ConnectionSpec       渠道 / 账号作用域 / 协议 / Endpoint / 凭据引用
  ├─ CapabilityContract   参数、思考、Structured Output、模态、限制
  └─ ModelOffering        model × connection × capability × priority
              │
              ▼
       RouteCatalog candidates
              │
       资格过滤 + 稳定优先级
              │
              ▼
       Frozen Session RouteBinding
              │
              ▼
       Request requested -> effective
              │
              ▼
       Reusable Protocol Adapter
```

运行时只接受 `status=published` 的自定义 `MODEL_CONTROL_PLANE_JSON`。API 与 Worker 都计算同一 `control_plane_hash`，并在 health / 启动日志暴露 revision/hash/status。完整的审批 UI、数据库草稿工作流不在本版本内；生产发布系统应只把已审核快照注入运行时。

## 3. 首批 AIHubMix 模型

内置供应关系覆盖：

| provider | model | protocol |
|---|---|---|
| `anthropic` | `claude-opus-5-5` | Claude Messages |
| `anthropic` | `claude-sonnet-5` | Claude Messages |
| `grok` | `grok-4.7` | Responses |
| `openai` | `gpt-6-luna` | Responses |
| `openai` | `gpt-6-sol` | Responses |
| `openai` | `gpt-6-astra` | Responses |
| `glm` | `coding-glm-5.3-free` | Chat Completions |
| `xiaomi` | `xiaomi-mimo-v2.6-pro-free` | Chat Completions |
| `gemini` | `gemini-3.8-flash` | Gemini Native |

`claude`、`google`、`gpt`、`zhipu` 可作为入口 provider alias，但 Session 内保存的是规范化 provider。

MiMo V2.6 Pro Free 当前只开放 `think_level=auto`：本版本没有足够稳定证据把调用方的 on/off/effort 偏好映射成真实上游执行语义，因此 Fail-Closed，而不是伪造能力。GLM/GPT/Grok/Claude 使用各自独立能力合同。

## 4. 多渠道优先级

优先级只在 Session 路由冻结前参与选择：

```text
同一 model 的多个 Offering
        │
        ├─ capability requirements
        ├─ channel allowlist
        ├─ server config / credential
        ├─ adapter / file adapter
        └─ deployment policy
        │
        ▼
按 priority DESC + offering_id 稳定排序
        │
        ▼
冻结 Session RouteBinding
```

高优先级 Offering 不合格时可以在冻结前选择下一合格候选。冻结后不做运行时 failover。

Session 新增 `capability_requirements`，可声明输入模态、Structured Output 保障、思考档位、必需 feature 和 channel allowlist。Request 超出冻结合同会在 Provider dispatch 前拒绝。

## 5. 自定义多渠道示例

自定义 Connection 可复用已有协议，无需增加模型专属分支。凭据只写环境变量名：

```json
{
  "status": "published",
  "mode": "merge",
  "revision": "relay-model-control-plane/prod-2",
  "release_metadata": {
    "release_id": "prod-2",
    "change_ticket": "REL-200"
  },
  "connections": [
    {
      "connection_id": "openai_direct_responses",
      "channel_id": "openai_direct",
      "protocol": "responses",
      "base_url": "https://api.openai.com/v1",
      "credential_env": "OPENAI_DIRECT_API_KEY",
      "account_id": "prod-primary"
    }
  ],
  "offerings": [
    {
      "offering_id": "gpt-6-sol-direct",
      "provider": "openai",
      "model_pattern": "gpt-6-sol",
      "connection_id": "openai_direct_responses",
      "capability_contract_id": "gpt-6-reasoning",
      "priority": 150,
      "observed_model_policy": "strict"
    }
  ]
}
```

若内置 AIHubMix Offering priority=200，则新 Session 先尝试 AIHubMix；只有它在路由资格检查阶段不合格时，才选择 priority=150 的 direct Offering。

## 6. 上游实际行为核对

Responses、Chat Completions、Claude Messages、Gemini Native 成功响应都会形成 `observed` facts。AIHubMix 场景额外读取可用的：

- 实际/最终模型；
- Router resolved model；
- fallback 标志；
- JSON repair 标志。

首批精确模型 Offering 默认 `observed_model_policy=strict`。如果可观测实际模型与冻结模型不一致，Relay 会在**原始 2xx 响应已经归档后**将本次 Request 判定为 Provider contract failure；不会自动切另一渠道重发。原始响应和 provider output object 仍可诊断。

Legacy Offering 默认保持 audit 语义，避免升级破坏旧 Session。

## 7. 能力与协议边界

- `ResponsesV2Adapter`：GPT/Grok 等可复用 Responses wire；能力差异来自冻结的 CapabilityContract。
- `ChatCompletionsV2Adapter`：GLM/MiMo 及后续已验证 OpenAI-compatible Chat 模型复用。
- `ClaudeMessagesV2Adapter`：保留完整 content blocks / thinking signature 等 transport history。
- `GeminiNativeAdapter`：继续承担 Gemini Native inference；既有 99 MiB Material transport / projection 机制不变。
- 新模型只要已有协议和 CapabilityContract 能完整表达，可通过发布配置接入；新协议、新模态投影或无法表达的执行语义仍需要平台代码升级。

## 8. 配额边界

`coding-glm-5.3-free` 与 `xiaomi-mimo-v2.6-pro-free` 的内置 Offering 带有可审计 quota metadata，但 2.0.0 **没有实现跨 API/Worker 实例的分布式共享额度扣减或 delayed queue**。因此 quota 字段在本版是 declarative governance metadata，不应被描述成强制限流器。

如果后续实现“额度恢复后再执行”的延迟调度，需要同时扩展 Worker wake/drain gate，不能把“当前不可 claim”误判成“未来没有工作”。

## 9. 升级检查

1. 沿用现有数据库；无需新 SQL。
2. API 与 Worker 使用同一镜像和同一 `MODEL_CONTROL_PLANE_*` 配置。
3. `/health` 对比 API/Worker 的 `control_plane_revision/hash/status` 与 `route_catalog_hash`。
4. 已有 Session 保持原冻结 RouteBinding；能力/渠道配置变更请创建新 Session。
5. 自定义 `MODEL_CONTROL_PLANE_JSON` 必须包含 `"status":"published"`。
6. 对真实账号逐一验证九个 `model × channel × protocol` 组合后再扩大生产流量。
