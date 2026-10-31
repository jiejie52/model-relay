# Model Relay 6.4.0 稳定材料跨阶段投影复用

基线：用户指定的 model-relay-6.3.0-source-final.zip，完整解压后修改。属于已有缓存处理方式增强，YY 升级为 6.4.0。API、Worker 同时升级；不新增 SQL、依赖或 tests 文件。

## 行为变化

1. 新受理 Request 的 ContextPlan 增加 material_reuse 段，独立记录有序材料内容及表示摘要。它不含阶段、当前问题和系统指令；原有完整前缀身份仍然保留。相同材料跨阶段保持材料指纹一致。材料顺序、重复项、文件名等表示差异按原语义保留。
2. Resolver 将材料候选信息写入 selection_evidence，但不把它当作 Provider 资源兼容证明，不用材料处理命中率替换模型命中率或经济性预测。
3. 对 Gemini 现有投影允许进入缓存的 inlineData 材料，首次读取并核验原字节摘要后，持久化确定性的 Base64 投影；后续阶段可由另一 API/Worker 实例读取该对象，跳过原材料读取与重复编码。持久化复用使用现有 relay_objects 和 StorageRegistry，无需本机进程缓存或新 SQL。
4. 派发仍消费当前绑定生成的完整材料描述，包括当前文件名、MIME、generation、账户及连接信息。派生对象仅保存可复用的内容编码，不保存旧阶段指令、签名 URL 或 Provider 缓存句柄。
5. 身份包含 tenant、conversation、Session、材料、源对象、实际内容摘要、投影版本、投影 MIME、连接和账户。变化会选择新对象。每次先通过原有归属查询和材料状态检查，删除/失败材料不会因存在旧派生对象重新可用。缺少内容身份时不持久化猜测结果。
6. 派生对象的内容校验或读取失败会重新读取原材料并核验；写入失败仍使用本次正确投影，并记录 not_persisted。原材料摘要不匹配仍硬失败。并发请求以同一内容地址幂等发布相同字节，无额外 Provider 副作用。
7. 日志 material_projection_reuse_observed 和结果 cache.material_reuse 分别显示 reused、built、not_persisted、reused_bytes。cache.material_fingerprint 显示独立材料指纹。观测不含正文或凭据，并在物理计划生成前剥离，避免复用计数变化污染内容指纹。

## 兼容与边界

- 已冻结且没有 material_reuse 段的 Request 继续原投影路径，不追加新开关；新受理 Request 自动使用本功能，调用方无需新增字段或统一系统指令。
- 不改变 Gemini layout/9、角色、消息边界、历史、模型参数或 Schema。隐式缓存仍发送完整请求，显式缓存仍验证原物理前缀和句柄；不移除同一次 Request 的冻结计划校验。
- 70 MiB 材料选择、Files API 与 External URL 的原有通道限制保持有效。explicit Request-only 材料因原消息边界不能安全拆分时仍不强行提升到 CachedContent；摘要中 material_count 为 0 代表本次没有可选的内联投影，不能解释为数据丢失。
- 本次具体复用对象是已有内联材料的确定性编码，没有引入新的通用解析器，也不跨材料 ID 或跨 Session 共享。文件绑定沿用原有效性管理。
- 派生对象纳入已有 Relay artifact 存储，通常约为原材料大小的 4/3 加元数据；内容或版本变化保留不同不可变对象，按部署现有 artifact 留存策略管理。源材料状态及映射检查独立于对象留存，旧派生对象本身不授予材料访问权。
- 系统指令不同可能使 Gemini 的 Provider 前缀缓存不兼容。本版可证明 Relay 材料处理复用，不承诺每次 Provider hit。cachedContentTokenCount 缺失仍 unknown，零仍 miss，正数才是 hit。
- CacheIntentPlan、CacheExecutionBinding、Session 隔离、create_unknown 对账、派发后不重发、不换机制等规则保持原样。本包未混入先前实际部署包中的 7/8 多绑定残留。

## 验证与部署

原有 125 项测试通过；API/Worker 导入及版本检查通过。包外定向验证覆盖跨实例复用、阶段独立身份、投影与旧路径一致、Session/账户/源对象/MIME/内容变更失效、派生对象损坏修复、原字节完整性错误、持久化失败继续正确推理、材料删除及 explicit 消息边界。

完整替换源码并同时重建 API、Worker 镜像；无需新增迁移。创建两个独立幂等键的业务请求，第二阶段应看到 reused > 0（前提是同 Session 且材料属于原投影允许的内联集合）；另查看 Provider 缓存 token，分开验收处理复用与推理命中。

尚未连接生产 Supabase、对象存储或真实 Gemini 进行计费调用；生产权限和真实缓存读取仍需部署后验证。
