# Model Relay 8.0.0 升级与配置发布

在项目根目录运行以下命令。保留原环境变量可继续使用内置兼容供应；数据库必须已执行 001–011，本版没有新增 SQL。API 与 Worker 同批升级。部署前排空旧进程正在运行的任务，已有结果与会话数据不要重写。

## Google 官方供应登记

`config/google_official.declaration.json` 是停用草稿。替换 account_id、project_id、deployment_id 等真实边界，在环境或密钥注入源设置 `GOOGLE_GEMINI_API_KEY`。JSON、Request 和普通日志只保存凭据引用，不能写入真实 API Key。

该声明的官方 Gemini 连接使用同一 gemini_native 实现，禁用网关亲和，采用 Files API 和 fileUri 显式缓存；Thinking 为预算投影。阈值与预算值仍需按具体模型真实调用复核，示例不是认证证据。为另一账号复制连接、Profile 和 Supply 并采用独立 ID 与 credential_ref 即可，无需改启动代码。

先确认目标合同，再将真实通过的 contract 与 Profile mechanism verification_status 更新为 verified，将拟启用 Supply 的 enabled 改为 true。生成待认证身份：

```bash
python -m app.governance.cli certification-input --source config/google_official.declaration.json --out certification-request.json
```

此输出保持 draft，不授予流量。受控验证流程必须真实执行推理、材料上传、缓存创建/用量、错误与故障恢复，按最终 supply_hash 填写通过的 capabilities、evidence_ref、reviewed_by 和带时区的 valid_until，生成独立的认证记录。任何被认证实体修改后都需要新的身份和对应证据。

```bash
python -m app.governance.cli declaration --source config/google_official.declaration.json --certifications certification-passed.json --revision release-8.0.0-1 --out candidate.json
```

候选仍为 review。外部审批记录包含 candidate_sha256、base_control_plane_hash、approved_by、approval_ref。candidate_sha256 使用编译模块 digest(candidate) 的规范 JSON 内容摘要，不是漂亮打印文件的字节摘要。

```bash
python -m app.governance.cli publish --candidate candidate.json --approval approval.json --out published.json
```

设置 MODEL_CONTROL_PLANE_FILE 为批准文件的绝对路径，MODEL_CONTROL_PLANE_FILE_SHA256 为该文件实际字节 SHA-256。API、Worker 部署同一配置。模型全部停用时受管模型范围仍保留，禁止绕回旧通配路由。

## 原子激活与保留旧版本

也可使用部署级指针代替两个直接文件变量。首次激活用空的 expected-current-sha256；以后使用当前指针的 sha256：

```bash
python -m app.governance.cli activate --pointer /config/active.json --release /config/published.json --expected-current-sha256 ''
```

设置 MODEL_CONTROL_PLANE_RELEASE_POINTER=/config/active.json。指针模式不能同时设置 MODEL_CONTROL_PLANE_FILE 或 MODEL_CONTROL_PLANE_JSON。激活不对运行中的 API/Worker 热加载；协调重启/滚动替换后，新进程使用新快照，新 Session 才获得新的选择依据。旧 Session 继续使用其冻结版本。指针自动保留历史 release 引用且不删除文件；回滚仍通过同一 CAS 指令激活旧批准文件。

直接文件模式可用 RETAINED_CONTROL_PLANE_FILES 指定需要保留的旧批准文件：JSON 数组，每项为 path 与 sha256。保留的文件只用于执行旧合同，不能成为新会话路由目录。历史批准文件、凭据引用与执行代码要保留到相关会话、Request 和资源 pin 全部退出。程序不自动退役旧版本或删除远端孤儿资源。

工作簿流程仍可用。supply-registry 可附 control_plane 对象登记新实体；启用缓存的 registry 条目需附 capability_certification，且默认/强制、TTL、门槛及准备失败策略不得扩大认证授权。声明式方式更适合登记完整新渠道。

## 必须在目标环境补充的验收

实际 Google/聚合渠道认证、文件处理断连与恢复、真实缓存命中和账单；原生 PostgreSQL 多连接下失租、锁等待、pin 原子回滚；跨节点分区；旧会话跨发布/回滚恢复。HTTP 模拟成功和配置编译成功不能代替这些验收。
