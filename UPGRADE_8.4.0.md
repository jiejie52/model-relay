# Model Relay 8.4.0 使用与部署

## 日常操作

1. 编辑 config/model_management_v2.xlsx。当前官方连接已登记，确认账号/项目标识和区域适合自己的凭据作用域。首次接入新的渠道时，在连接、协议、能力和供应页增加记录，并在模型、路由、缓存页设置实际意图。
2. 在 API 与 Worker 注入同名环境密钥，例如 GOOGLE_GEMINI_API_KEY。也支持私有 .env。两端使用同一工作簿、DEPLOYMENT_ID 和已有 Supabase 配置。
3. 重新部署。没有手工编译、审核 JSON、发布指针或初始化快照文件这一步。

生产密钥不得写入 Excel、源码或普通日志。新增账号应采用独立连接 ID 和账号/项目标识，避免跨账号复用历史句柄。原 CONNECTION_AVAILABILITY_MODE=allowlist 若仍存在，应将新连接加入允许列表，或恢复默认 all；该旧部署限制仍会被执行，不会被 Excel 越权覆盖。

## 九张配置表

模型清单：对外模型身份、期望模态、思考、结构化输出和申请发布意图。

渠道优先级：各环境的渠道侧模型 ID、启用、顺位和不可用策略。1 最优先；停用供应不能接收新流量。

缓存设置：机制、默认/强制授权、门槛、TTL 和准备失败策略。当前包沿用停用意向；打开启用后，自动验证对应机制，不会仅因表格勾选就宣告验证通过。

填写说明：基本操作提示与保留的模型来源信息。

环境映射：环境名称到 DEPLOYMENT_ID 的映射，以及 Excel 接管的提供方列表。当前“测试→railway，管理 gemini”表示 Excel 全面接管该部署的 Gemini；多个提供方以顿号或逗号分隔。未接管范围保留兼容种子，方便原有部署平滑迁移。

连接配置：连接/渠道 ID、协议族、账号项目区域、REST/SDK/上传端点、鉴权方式和凭据引用。

协议配置：协议族、允许主机、可选亲和头及响应证据头。只复用已注册协议实现，不接受 Python 类路径、脚本或原生报文模板。

能力合同：输入、思考映射/预算、结构化保证、输出上限、材料传输、外链阈值、显式缓存源模式及可选采样范围。空采样范围表示该选项不开放。预算仅用于 budget 投影，level 模式可以留空。

供应配置：将模型、环境、渠道关联到具体连接、协议与能力 ID。表头在第5行；配置从第6行开始，解析器扫描实际记录而不依赖旧表格的固定行数。引用下拉覆盖到1000行，超过时可扩展 Excel 验证范围。

## 当前 Gemini 配置

六个既有目录型号申请经官方连接提供：gemini-3.6-flash、gemini-4-argon、gemini-3.8-flash、gemini-3.5-flash-lite、gemini-3.1-flash-lite、gemini-3.1-pro-preview。名称和期望能力沿用 8.1.0 目录，不代表已经确认它们在你的官方账号可用。自动验证会将实际不可用的型号保持 pending，并报告具体渠道行号；不能用模拟结果或模型名称代替真实可用性。

官方默认使用 Files API，禁用网关亲和。所有旧 Gemini 聚合供应（含 Native、Chat 和通配）在编译中停用。-search、-free 是原聚合别名，未猜测官方映射，继续停用；需在表中登记经过验证的渠道侧模型与相应语义后再启用。

## 自动验证与内部数据

系统在现有 Supabase 桶的 RELAY_STORAGE_PREFIX/configuration/ 下自动保留历史运行配置和验证观察，不增加 SQL 表、不要求用户维护内部文件。当前配置的完整身份与源 Excel 摘要进入日志，实际就绪状态独立显示。只读查询/保存内部状态失败会阻止新的受管供应接流量，但不会把服务启动变成异常退出，也不会回退到旧聚合渠道。

新供应或身份变化时，会执行有时限的真实部署探测：Gemini 推理、代表性思考投影、声明为原生时的简短 JSON Schema 输出、Files 上传/引用；启用显式缓存时检查 countTokens、fileUri 创建与带缓存推理及用量；隐式缓存需要实际命中观察。探测可能产生少量真实调用费用。临时远端资源会尝试删除，缓存资源同时有 TTL。默认缓存停用不会创建显式缓存资源。

成功观察按完整连接、模型、合同、Profile、上游映射、执行授权和凭据身份签名，默认复用24小时；单纯调整顺位不会使未变的能力身份失效。缓存创建探测使用独立持久化操作标记：超时/未知结果不自动重复 POST；诊断会显示 cache_validation_operation_busy_or_unknown。资源已知时可受控核对后退役相应验证操作；不能靠删除未知记录来强行重建。此边界不要求正常渠道接入另走人工发布流程。

自动探测是必要功能的实测观察，不是对所有参数组合、模态、负载和故障的穷尽证明。当前非 Gemini 已有协议支持基础文本/结构化探测；尚未实现验证的资源组合保持 pending，不会虚标已验证。需要扩展新的协议行为时仍属于平台能力升级。

## 看哪里是否生效

/health 的 ok 表示进程存活，configuration.state 表示配置状态，仅公开摘要。API 与 Worker 均提供需要 Authorization: Bearer <RELAY_API_TOKEN> 的 /configuration；其中 supplies 逐条报告 ready、pending、disabled，包含工作表/行号、连接、渠道、主机和原因。比较两端 configuration_hash，确认采用同一 Excel 和编译器；control_plane_hash 还反映各自就绪供应状态，若不同应检查凭据、网络和验证结果，不能把不同就绪状态声称为完全一致。

常见原因：credential_missing 表示缺少凭据；upstream_http_404 常表示该型号在当前端点不可用；upstream_http_401/403 表示鉴权/权限问题；snapshot_history_unavailable 表示内部历史存储暂不可用。修改对应 Excel 行或环境配置后重新部署即可重新检查。旧 MODEL_CONTROL_PLANE_FILE/JSON/RELEASE_POINTER 不再是两端当前目录入口。

可选本地结构检查：python -m app.governance.cli validate。可选实际部署诊断：python -m app.governance.cli diagnose，后者可能执行上述真实探测，不是必需的发布步骤。

## 可靠性与兼容

Excel 仅在部署初始化时读取；请求热路径仍使用只读配置及冻结合同。新配置影响新 Session，旧 Session / Request 按历史连接版本继续执行，不重写其身份。历史配置自动读回并只用于装配旧客户端，不能进入活动新会话目录。原双围栏、发送封存、dispatch_started 后禁重发和 result_stored 只提交恢复保持原路径。

源表结构错误时，两端仍提供健康和配置错误信息，新调用不使用不可靠目录。旧 /v1/jobs/Dify facade 继续属于原 legacy 兼容路径；本次治理针对 v2 主链，不额外改写旧入口。
