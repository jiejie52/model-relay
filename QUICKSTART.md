# Model Relay 0.4.1 部署关键操作

## 1. 从 0.3.1 升级数据库

现有数据库已经执行过 `001/002/003` 时，**只执行新增 migration**：

```text
sql/004_provider_native_file_ingress.sql
```

全新数据库才按：

```text
001_relay_schema.sql
002_fusion_runtime.sql                 # 仍需旧 Fusion 时
003_relay_v2_session_request_material.sql
004_provider_native_file_ingress.sql
```

`004` 会让 `relay_materials.object_id` 可为空，并新增 fallback、binding attempt、account scope/generation 与 Request binding snapshot。不要重复执行 001/002/003 覆盖现有生产结构。

## 2. Gemini / Railway 配置

如果 Railway 需要 AIHubMix Gemini Native：

```text
DEPLOYMENT_ID=railway
EXECUTION_POOL=railway-default
WORKER_EXECUTION_POOLS=railway-default
ENABLED_CONNECTIONS=aihubmix_default,aihubmix_gemini_native

AIHUBMIX_API_KEY=...
AIHUBMIX_GEMINI_BASE_URL=<WF-NormalInference 中已验证的 Native Proxy base URL>
AIHUBMIX_GEMINI_CONNECTION_ID=aihubmix_gemini_native
```

`AIHUBMIX_GEMINI_BASE_URL` 不在代码里猜测，必须使用你当前已验证工作流中的值。Relay 会调用：

```text
POST <base>/upload/v1beta/files
POST <x-goog-upload-url>        # upload, finalize
GET  <base>/v1beta/<file.name>  # PROCESSING 时轮询
POST <base>/v1beta/models/<model>:generateContent
```

## 3. Kimi / SAE 配置

```text
DEPLOYMENT_ID=aliyun-sae
EXECUTION_POOL=aliyun-default
WORKER_EXECUTION_POOLS=aliyun-default
ENABLED_CONNECTIONS=moonshot_official

MOONSHOT_API_KEY=...
MOONSHOT_BASE_URL=https://api.moonshot.cn/v1
MOONSHOT_CONNECTION_ID=moonshot_official
```

若使用国际站/其他官方 endpoint，修改 `MOONSHOT_BASE_URL`，不要复用不属于同一 account scope 的 file ID。

## 4. Supabase 的新角色

下面这些 **仍使用现有 Supabase Artifact Storage**：

```text
request snapshot / raw response / raw error / session history /
provider-derived Kimi extraction / Fusion artifacts
```

只有“原始输入文件 payload”改为 native-first。成功的 Gemini/Kimi native upload 默认不创建 Supabase input-file fallback。

## 5. Material API 新调用方式

上传时必须告诉 Relay 目标 connection，才能走 native file path。

### Gemini

```text
POST /v2/materials
Idempotency-Key: <stable-key>
Content-Type: multipart/form-data

file=<binary>
tenant_id=...
conversation_hash=...
target_connection_id=aihubmix_gemini_native
durability_policy=native_first
fallback_policy=on_provider_unavailable
```

成功响应重点：

```json
{
  "schema_version": "relay-material/2.1",
  "status": "ready",
  "durability": "provider_bound",
  "ready_for": ["aihubmix_gemini_native"],
  "fallback": {"stored": false, "object_ref": null},
  "provider_binding": {
    "provider": "gemini",
    "state": "active"
  }
}
```

### Kimi

把 `target_connection_id` 改成：

```text
moonshot_official
```

Relay 根据 MIME 在 Adapter 内决定：

```text
PDF/DOC/DOCX/TXT/MD/... -> purpose=file-extract -> GET /files/{id}/content
image/* (SVG 除外)      -> purpose=image        -> ms://<file_id>
video/*                  -> purpose=video        -> ms://<file_id>
```

## 6. durability / fallback 策略

默认：

```text
durability_policy=native_first
fallback_policy=on_provider_unavailable
```

含义：Provider native 成功时不存原始输入文件；只有明确的暂时不可用窗口（连接/超时/408/425/5xx/Provider processing timeout）才允许 Supabase 接住文件。`429` 默认返回 Provider 原始错误，不自动改变留存策略。

如果业务明确要求长期可重绑/跨 Provider：

```text
durability_policy=relay_backed
```

此时 native 上传成功后仍会额外保存一份 fallback 原始字节。

## 7. 启动

```bash
uvicorn app.api:app --host 0.0.0.0 --port 8000
python -m app.worker
```

健康检查：

```text
GET /health
version = 0.4.1-observability
```

## 8. 最少验收

```bash
python -m compileall -q app
python -m unittest discover -s tests -v
```

预发布再做 4 个真实测试：

1. Gemini PDF native upload 成功后，Supabase input-file fallback 无新增对象。
2. Kimi PDF 经 `file-extract -> /content` 后参与模型请求；file_id 不直接作为文本上下文。
3. Kimi image/video 请求使用 `ms://<file_id>`。
4. 故意触发 Gemini/Kimi Files API 4xx/5xx，核对 raw error body/headers/phase 不截断。


## 9. 0.4.1 日志配置

0.4.1 **没有数据库 migration**。从 0.4.0 升级只需要重新部署 API/Worker。

默认建议：

```text
LOG_LEVEL=INFO
DEPENDENCY_HTTP_LOG_LEVEL=WARNING
UVICORN_ACCESS_LOG=false
```

这样会隐藏 `httpx`/`httpcore` 的高频 Supabase REST/RPC 成功日志，例如 Worker 每 2 秒调用一次 `claim_relay_job_v2` 的 `HTTP/1.1 200 OK`，也不会让 Uvicorn 的 status/result 轮询 access log 淹没业务日志。

Relay 自己保留以下关键事件：

```text
request_accepted
job_claimed
request_execution_started
material_bindings_frozen
provider_call_started
provider_call_completed / provider_call_failed
upstream_stream_interrupted
request_execution_committed
request_executor_failed / request_executor_timeout
provider_file_binding_started / completed / failed
material_provider_fallback_activated
```

关键事件均尽量带 `request_id/session_id/job_id`、`provider/connection_id/model`、`phase`、`duration_ms`、`http_status`、`failure_class` 与 Provider request id。异常事件使用 `exc_info` 输出堆栈。日志不会打印 Authorization/API Key/Token，也不会打印请求正文或原始错误 body。原始错误仍通过 Raw Error 通道读取。

常见 `failure_class`：

```text
client                 # Relay API 4xx / 调用方参数或状态问题
upstream_rejected      # Provider 返回 4xx
upstream               # Provider 返回 5xx/处理失败
upstream_timeout       # Provider/Files API 超时
upstream_transport     # DNS/TLS/连接/流中断
relay_validation       # Relay/Adapter 校验失败
relay_configuration    # 连接未配置
relay                  # Relay 内部异常
```

若临时需要观察底层 HTTP 调用，可把：

```text
DEPENDENCY_HTTP_LOG_LEVEL=INFO
```

调试结束后建议恢复 `WARNING`。
