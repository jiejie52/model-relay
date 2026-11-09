# 8.3.0 配置发布与部署

## 配置的唯一职责

此次 Gemini 渠道切换以 config/google_official.declaration.json 为权威声明输入。它完整登记一条官方连接、独立的官方 Profile、缓存策略、九条精确模型 Supply 及逐型号能力合同。route_policies 中的渠道/主机授权与不可用策略在离线编译时生效，随快照发布，不再从执行面 Settings 另建一套策略。

七条申请启用的官方供应：gemini-3.6-flash、gemini-4-argon、gemini-3.8-flash、gemini-3.5-flash-lite、gemini-3.1-flash-lite、gemini-3.1-pro-preview，以及沿用原示例的 gemini-2.5-pro。它们的能力声明来自既有目录和原官方示例，属于待认证的目标合同，不表示已确认这些型号在你的官方账号可用。

gemini-3.1-pro-preview-search、gemini-3.5-flash-lite-free 均已登记在官方治理范围，但保持 enabled=false。二者是原聚合别名，没有凭空添加工具或免费承诺。取得经验证的官方模型/工具/计费等价映射后，修改相应合同、metadata.upstream_model 和 enabled，重新认证发布即可；无需修改 Python。

原工作簿作为历史作者输入完整保留，本次发布不读取它。以后若选择工作簿 authoring，需要通过配套受认证 supply registry 编译发布；不要将工作簿和声明文件同时当成正在生效的目录。运行时只消费批准 JSON。

## 1 准备连接与真实证据

在项目根目录操作，使用目标部署的 DEPLOYMENT_ID、AIHubMix/Moonshot 连接 ID 和端点等原配置。声明中的 deployment_id 默认 railway，其他部署应同步修改。填入官方 Connection 的真实 account_id 与 project_id，替换 REPLACE_WITH_ACCOUNT / REPLACE_WITH_PROJECT；不能用占位值认证或发布。

将 GOOGLE_GEMINI_API_KEY 通过部署平台注入为 API 和 Worker 的进程环境密钥。任意 credential_ref 由通用解析器按环境名称解析，不能写入声明、快照、认证 JSON 或普通日志，也不能仅把未声明的动态密钥名称放到 .env 后假定它会成为 Settings 字段。

```bash
python -m app.governance.cli certification-input --source config/google_official.declaration.json --out certification-request.json
```

该文件只给出七条启用申请的 draft 身份、目标 supply_hash 及必需能力列表，不授予流量。它描述认证通过后即将发布的精确合同；源文件中的 unverified 状态不会被原位修改。

由受控验证流程按每条精确供应验证推理、结构化输出/思考、Files API、显式 fileUri 缓存、隐式缓存、用量、授权证据与恢复边界。对实际通过的目标 supply_hash，生成由 offering_id 索引的独立 certification-passed.json，记录 status=passed、evidence_ref、reviewed_by、带时区的 valid_until 以及通过的 capabilities。代码未提供假造通过记录的命令或生产认证文件。不能通过简单修改 draft.status 代替真实验证。

若某个模型在账号下不可用或能力未通过，调整其合同或停用该 Supply，再重新生成身份与真实认证。已验证的聚合能力不能当作官方认证复用。

## 2 编译完整快照

```bash
python -m app.governance.cli declaration --source config/google_official.declaration.json --certifications certification-passed.json --revision gemini-official-8.3.0-prod-1 --out candidate.json
python -m app.governance.cli approval-input --candidate candidate.json --out approval-request.json
```

candidate.json 为 review，包含完整的 mode=replace 快照，保留非 Gemini 供应，停用全部原 Gemini 聚合供应，包括通配和 Chat 供应。所有候选能力仍须证据匹配；认证后的状态只在编译产物内形成。

approval-request.json 包含规范 candidate_sha256 和 base_control_plane_hash。交给外部受控审核流程，在审查通过后形成带有真实 approved_by、approval_ref 的 approval.json；模板不是审核通过记录。

## 3 发布与激活

```bash
python -m app.governance.cli publish --candidate candidate.json --approval approval.json --out config/runtime/gemini-official-8.3.0.published.json
python -m app.governance.cli activate --pointer config/runtime/active.json --release config/runtime/gemini-official-8.3.0.published.json --expected-current-sha256 '' --relative-paths
```

空 expected-current-sha256 仅用于首次激活。已有指针时使用当前 active.json 的 sha256；过期基准会拒绝激活。relative-paths 让发布目录可以随镜像或配置卷迁移。激活前 CLI 会按真实部署设置校验待加载文件；draft、错误摘要或违反发布路由策略的文件不能激活。

将同一份已批准发布文件和 active.json 配送到 API 与 Worker 的 config/runtime/。旧发布文件及指针 retained 引用的文件也必须完整保留。两服务共享 config/control_plane.bootstrap.json，默认无需额外设置模型或渠道专属启动参数。也可同时设置相同的 MODEL_CONTROL_PLANE_FILE、MODEL_CONTROL_PLANE_FILE_SHA256 走直接文件模式，此时清除冲突的 MODEL_CONTROL_PLANE_JSON / MODEL_CONTROL_PLANE_RELEASE_POINTER。

重启两服务后生效。程序只读取一次激活指针，再以同一已固定版本装配推理、文件、缓存与历史连接，避免装配期间指针切换导致混装。运行中没有自动热加载或静默降级至内置目录。

## 4 确认实际加载而非只看版本号

```bash
python -m app.governance.cli status
```

输出应显示 source=pinned_release、正确的 release_file_sha256、control_plane_hash、snapshot_id、route_revision；所有 enabled=true 的 Gemini Supply 都是 google_official，旧聚合供应 enabled=false。configured=false 会指出该连接凭据或端点尚未就绪，状态命令不会发起上游探测。

API 和 Worker 启动时均记录 control_plane_release_loaded，/health 均返回 control_plane_release。将两端健康响应分别保存为 api-health.json、worker-health.json，可执行：

```bash
python -m app.governance.cli compare-health --api api-health.json --worker worker-health.json
```

只有文件摘要、快照摘要、快照 ID 和路由版本一致才返回 matched。两个本地路径不同但文件内容相同是允许的；不需要为了比较而传递密钥。

新建 /v2/sessions，确认 session_route_frozen 的 channel_id=google_official，并检查实际 provider_http_wire 主机为 generativelanguage.googleapis.com。仅有 GOOGLE_GEMINI_API_KEY 或已改 Excel 均不等于快照已加载。

## 旧会话与兼容路径

旧 Session / Request 保持原冻结供应和连接身份，不因配置发布换渠道；retained 发布文件与可选种子连接只服务历史合同，不能进入新会话的活动目录。需要全量停止旧聚合流量时，调用方应停止续用旧会话并创建新会话；不得重写既有记录。result_stored 仍只提交恢复。

/v1/jobs 和旧 Dify facade 仍是设计文档中独立的 legacy 兼容路径，不自动升级为受治理 v2 Session；本版没有额外拦截或重写旧接口。此次配置治理与验收针对 v2 主链。

## 验收边界

本版已通过配置 A → 编译/发布/激活 → 真实适配器模拟出站，以及配置 B → 再发布 → 新会话切换连接的受控验收，整个切换过程 Python 文件摘要不变。模拟认证和上游响应仅存在临时验证目录，未装入此包。真实账号认证、真实缓存命中与账单、数据库多节点故障注入必须在目标环境完成。
