# Model Relay 8.3.0

修改基线为用户指定的 model-relay-8.1.0-source.zip，SHA256 为 aa8b98afc0c1d768e5f0ff08e3bee5d8fb3a5cd4269871972a7a8851e515f622。从该包重新完整解压，没有带入 8.2.0 的额外 Settings 渠道限制及 v1 入口拦截。按用户指定命名为 8.3.0。

## 受治理配置

google_official.declaration.json 从单型号停用示例补全为九条逐型号 Supply、逐型号 CapabilityContract、一条独立官方 Connection、独立 Profile、CachePolicy 和受治理路由策略。七条为申请启用；两个聚合专属别名暂停并保留精确模型范围。声明中的 enabled 是发布意图，认证与审核前不产生生产流量。

通用 route_policies 在离线编译中停用不被授权的渠道供应并冻结不可用行为。本配置将五条旧 Gemini AIHubMix Supply（含通配、Native 和 Chat）全部停用，保留非 Gemini 供应。没有在执行面写 Gemini 专属判断，也没有复制协议适配器。

## 编译与发布

声明编译产出完整 mode=replace 候选，避免 merge 后内置供应意外残留。首次发布以兼容种子初始化，后续发布以已选择的批准快照为基线，保留既有自定义实体及审核证据引用。受认证的目标身份覆盖 Connection、合同、Profile、CachePolicy、渠道侧模型及执行授权。认证输入为未来精确合同的 draft 身份；只有独立通过证据匹配时才允许生成 review 候选，认证状态变化不会使刚通过的身份失配。源文件保持未验证状态，不原位写入假认证。

批准与激活继续要求外部审核记录和文件摘要。激活前按目标配置验证发布文件，CAS 防止过期激活。新增可迁移相对路径模式。完整 replace 发布在运行时独立于节点的兼容种子目录，但基准摘要仍保留于审核元数据；已有 merge 兼容发布保留原基准一致性检查。

同时修复工作簿 registry 扩展输出时遗漏新增 CapabilityContract 定义的问题。

## 统一加载与诊断

API / Worker 使用同一个通用加载器，启动时仅解析一次指针，固定相同文件摘要后再装配当前及历史连接。默认 bootstrap 要求批准文件；缺失时清晰报错，不运行旧目录冒充新配置。历史种子连接仅服务已冻结旧合同，不参与活动路由目录。

增加 approval-input、status、compare-health CLI。两端启动日志及健康响应报告发布来源、文件摘要、快照 ID、内容摘要和路由版本；诊断列出模型、渠道、端点、启用和凭据就绪状态，端点查询参数与用户信息会脱敏。

## 职责与兼容

执行面的 RouteResolver 与 8.1.0 字节一致。运行时、协议适配器、Request 身份、SQL 和依赖保持不变；工厂只增加历史种子连接的通用保留。旧会话继续冻结原供应，既有 result_stored 恢复和 dispatch_started 禁重发边界不变。

没有新增 tests 文件。真实官方账号认证及生产故障验收需在目标环境完成；交付包不包含伪造的 passed 证据、批准快照、真实密钥或临时验证脚本。
