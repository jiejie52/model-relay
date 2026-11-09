# Model Relay 8.6.0

以 8.5.0 完整源码包为基线，按《代码评审》五项实施方法补齐契约和执行边界。本版属于处理方式增强，不改业务模型或渠道专用路由。原文件全部保留，没有新增 SQL 或依赖；已有 SQL 012 的适用条件不变。

## 部署

API 和 Worker 同时升级，使用同一工作簿与凭据。仍是修改 Excel、配置环境密钥、重新部署的流程，无需手工生成发布 JSON。原优先级、聚合停用及拒绝回退意图保持不变。官方缓存仍按原表停用；需要时在缓存设置中启用并让目标环境完成相应验证。

「协议配置」新增 J–L 可选列：证据来源、预期计费层级、要求原生约束证据。旧九列表兼容，留空沿用 gateway_headers 与原审计策略。交付表中的官方 Profile 配置 first_party_native_payload、official_direct、是。聚合 Profile 应单独登记并使用相应响应头证据，不能机械复制官方 Profile。

## 缓存材料预算

explicit_cache_material_mode 由控制面解析和校验，并在运行时 CacheExecutionBinding 中显式表达。inline_bytes_only 保持原预算行为；file_uri_allowed 仅对已经绑定为 gemini_file_uri 的材料使用引用开销计量。外部 HTTPS URL、内联数据及没有实际 URI 的占位记录不能获得该豁免。

每条引用按至少 512 字节计量；较长 URI 按实际序列化元数据大小加封装开销计量。材料排序仍使用原始大小和语义类别，消息顺序不因预算排序改变。未进入缓存集合的材料留在完整推理上下文中。原始材料大小、哈希和身份不改写。

本地 80 MiB PDF 用例验证的是引用预算与实际运行时缓存投影，不代表 Google 接受该大小或格式的文件。供应商文件大小、页数、Token、地区与账号限制仍需目标环境验证。

## 原生响应证据

first_party_native_payload 是由受治理配置明确授权的证据采信模式。协议适配器结合冻结 Profile、已授权 HTTPS 主机、实际发送的 responseSchema、响应 modelVersion、usageMetadata、正常 STOP 完成原因及未经修复的原始 JSON / Schema 校验形成观察事实。截断、无 Schema、JSON 修复、负面协议证据、模型字段缺失等情况不得确认 native_constraint_enforced。

结果合同继续在 ResultCommitter 路径核验这些事实。官方配置启用 require_native_constraint_evidence，不再靠关闭该要求规避检查。SDK Files / Cache 资源成功不会被当作推理原生约束证据。

official_direct 表示直连来源，不是价格或免费/付费账单证明，也不是供应商签名的独立证明。要求 free / paid 且拒绝未知计费时，不能用 official_direct 冒充已证实的计费层级。最终 Canonical Schema 后置校验仍然保留。

## 材料资格

Session 创建前按候选连接、账号作用域、有效期、就绪状态及允许传输类型检查绑定。已有 External URL 绑定不能满足 Files-only 官方合同。目标绑定不兼容且没有保留字节时，候选被记为 MATERIAL_UNREACHABLE_FOR_SCOPE，再按 try_next / reject_new_session 决策。

durability 标签本身不是可重绑证明。当前通用运行时没有持久化外部源的重新鉴权下载合同，不能仅看到 source_recoverable 或 source_url 就允诺重建；必须存在目标有效绑定或可供现有绑定器读取的保留字节。此时应重新上传或保留 Relay fallback，不会将不可消费材料的 Session 先冻结到官方再失败。

## 连接指纹与旧合同

新 RouteBinding / Request 带 connection_contract_version=2、connection_spec_hash、account_scope_id。FrozenExecutionPlan 和 SealedDispatchBundle 顶层使用字符串字段，强制校验 SHA256 格式、Session 与 Request 一致性、已注册客户端身份，并将字段纳入发送前 seal_hash。

新合同遗漏字段、未知版本、篡改指纹或作用域都会拒绝执行。纯检查逻辑位于共享契约模块，不让执行面依赖运行时实现。已有 Session / Request 无版本标记时保持原字段、摘要算法及恢复语义，不在加载时追加新字段或重算历史身份。

## 上传和日志

SDK 的 start、upload、finalize 使用连接工厂注入的同一受控传输。每次 HTTP 发送前、流的每个块发送前及流结束时检查当前 Request / Operation 围栏；失租事件还会取消正在等待的传输任务，阻塞期间有 Operation 时按有界间隔复核操作租约。上传完成后再次检查权限，失租资源不作为本地有效文件发布。

取消不能撤销已到达上游的字节或已发生的远端副作用。未知结果继续禁止盲目重建和重发，沿用原恢复合同。动态 upload_url 必须在声明主机范围内。

URL 清洗函数覆盖 key、api_key、access_token、upload_id、签名等参数及 URL 用户信息；实际 wire 日志继续移除整个 Query 和 Fragment，保留原本更严格的默认脱敏策略。

## 静态校验与验收

控制面基础验证、Excel 和历史快照编译入口共用 fileUri / Files / 文件适配器 / 显式缓存协议交叉校验。残缺合同在装配前被拒绝。

验收明细见 ACCEPTANCE_8.6.0.json，评审逐项对应见 REVIEW_RESOLUTION_8.6.0.md。Provider HTTP 为受控模拟；上传用例使用实际 google-genai SDK 和受控底层传输。SQL 验证使用本地 PGlite PostgreSQL 单连接引擎，不代表生产 Supabase、多客户端锁竞争或真实 Provider 认证。包内不包含临时验收脚本或模拟认证记录。
