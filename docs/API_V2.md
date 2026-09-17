# Relay v2 Session API

所有接口都需要：

```http
Authorization: Bearer <RELAY_API_TOKEN>
```

除了创建 Session 外，Session/Job 读取和 Job 提交还需要 owner headers：

```http
X-Tenant-Id: <tenant_id>
X-Conversation-Hash: <conversation_hash>
```

创建 Session 和创建 Job 使用各自稳定的 `Idempotency-Key`。

## 1. 创建 Kimi Session

```http
POST /v2/sessions
Idempotency-Key: session:<stable-business-id>
Content-Type: application/json
```

```json
{
  "tenant_id": "app-a",
  "conversation_hash": "conv-hash-a",
  "provider": "moonshot",
  "upstream_profile": "moonshot-official",
  "history_mode": "append",
  "defaults": {
    "model": "kimi-k3"
  },
  "context": null,
  "metadata": {
    "caller": "example"
  }
}
```

创建后 Session 固定 Provider/Profile/Protocol/History Codec。跨厂商续问应显式创建新 Session。

## 2. 提交增量 Job

```http
POST /v2/sessions/{session_id}/jobs
Idempotency-Key: turn:<stable-turn-id>
X-Tenant-Id: app-a
X-Conversation-Hash: conv-hash-a
```

K3 示例：

```json
{
  "input": [
    {
      "role": "user",
      "content": "请用一句话回答：1+1等于几？"
    }
  ],
  "generation": {
    "reasoning": {
      "effort": "high"
    }
  }
}
```

`input` 是本轮增量，不是客户端重传的权威历史。Session 历史由 Relay/Provider Codec 管理。

## 3. 查询 Job

```http
GET /v2/sessions/{session_id}/jobs/{job_id}
```

## 4. 读取结果

```http
GET /v2/sessions/{session_id}/jobs/{job_id}/result
```

- `queued/leased/running/prepared`：HTTP 202，Envelope 中仍是当前 Job 状态。
- `succeeded`：HTTP 200，`result` 为 compact result。
- `failed/cancelled/expired`：HTTP 200，`error` 描述真实终态。

异步查询的 HTTP 200 表示“成功读取 Job”，不代表 Provider 调用成功。

## 5. Provider Raw Error

失败示意：

```json
{
  "schema_version": "relay-envelope/2.0",
  "request_id": "req_x",
  "session": {"id": "..."},
  "job": {"id": "...", "status": "failed"},
  "result": null,
  "error": {
    "origin": "provider",
    "provider": "moonshot",
    "http_status": 429,
    "response_headers": [["content-type", "application/json"]],
    "body": {
      "encoding": "utf-8",
      "data": "<provider original body>"
    },
    "body_ref": "raw-error",
    "byte_length": 123,
    "sha256": "...",
    "received_complete": true
  }
}
```

Relay 不把 Provider 429 改成统一 `UPSTREAM_*` 错误码。Body 过大时不截断成摘要，使用：

```http
GET /v2/sessions/{session_id}/jobs/{job_id}/error/raw
```

取得完整原始字节。

## 6. 取消

```http
POST /v2/sessions/{session_id}/jobs/{job_id}/cancel
```

## 7. Compatibility

旧接口仍保留：

```text
/v1/jobs
/v1/dify/relay
```

它们位于 `app/compat/`，只是协议/字段兼容层。Core 不依赖 Dify/Fusion 业务对象。
