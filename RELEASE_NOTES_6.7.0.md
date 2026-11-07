# Model Relay 6.7.0 上下文复用增强

## 版本与执行边界

本版以指定的 6.6.0 源码包为基线，在完整副本中修改。版本按处理方式增强升级至 6.7.0。Session 供应边界、公共调用方式、Request 幂等、资源与执行双 fencing、dispatch seal、模型单次派发和结果恢复协议继续沿用。

新受理的 v66 Session 请求冻结 `cache_upgrade.version=3`、`fingerprint_version=2` 和历史冻结版本。已经受理的请求继续按自己的原快照执行；旧 upgrade/1、upgrade/2 的上下文计划、物理布局及指纹算法保持原路径。缓存头、实际命中与费用仍是独立的上游观测事实。

## 语义不变的复用

- 只规范化结构化对象、已经声明为无序的工具集合及 JSON Schema 的 required 集合。用户字符串、代码缩进、消息角色、轮次和 Part 顺序保持原值；不会删除业务文本中的 timestamp、request_id 或 trace_id。
- 历史每轮保存规范化后的原生 transport_history 及原生块摘要。下一轮直接读取这些原生块，检查冻结摘要及受理 ContextPlan 对应的逐轮内容身份；不按本轮采样、思考或阶段参数重新生成历史文本。旧历史在新请求中建立冻结校验事实，原对象不被覆盖。
- ContextPlan 分开记录系统、工具、各材料、各历史轮次和当前输入。上下文语义指纹排除执行身份、存储对象 ID 和采样参数；请求逻辑身份仍由原 request_identity 覆盖完整执行语义。

## 隐式基座与 Grok

- 隐式候选保存自己的机制配置、资格、门槛和计划 hash，不再只从主机制推导一个启用标记。Gemini 仍遵循最终显隐互斥。
- 当自动选择的显式前缀没有合法 CachedContent.contents 时，只有冻结计划已授权且隐式前缀通过独立资格检查，才能在派发前使用隐式绑定。显式强制机制不会被改为隐式机制。网络、鉴权及未知创建副作用不能借此改道重发。
- 新请求的 `legacy_v1` Grok 分支绕过新版 CacheOrchestrator 准备，但保留材料冻结、Request 执行权、绑定封存和单次派发。原 Responses 历史、加密 reasoning、store=false 和缓存键路径保留。环境或供应合同开关由新受理请求冻结；环境变更仍需重新加载 API 与 Worker。

## 双重指纹

- 新版物理链按最终规范化 JSON 的真实字段顺序及有序消息、Part 边界生成；结构标点也参与局部与链式哈希，字节分段拼接等于实际发送实体。
- 节点保存语义摘要、局部物理摘要、链式摘要、字节区间、估算 Token 和材料来源。材料 URI 变化而 Canonical Material 不变时，可识别为 wire_projection_changed。
- LCP 是合法分段边界处的公共前缀，不能解释为 Provider 的真实 Token 命中量。跨指纹格式的比较标记为不可比较，不计入连续 warm miss；多模态 Token 资格与收费判断仍用供应计量。
- 最终实体 SHA-256 与原子封存的 Adapter 输入摘要分别保留。Transport 诊断不构成数据库对网络实体的原子签名承诺。

## DOCX、PDF 与视频

DOCX 结构化文本桥接由具体供应的 CapabilityContract.metadata 明确授权，配置示例：

```json
{
  "material_projections": {
    "docx_structured_text": {
      "verification_status": "verified",
      "version": "relay-docx-structured-text/1"
    }
  }
}
```

将该片段配置在 CapabilityContract 的 metadata 内，通过现有控制面发布，再用新 Session 采用新冻结合同。配置不能代替具体供应的真实认证。已有冻结合同不会因部署新代码而自动获得授权，调用方无需修改 Prompt 或原生缓存参数。

- 桥接保留段落顺序、标题样式、表格单元格及合并信息、文字属性、链接和页眉页脚、脚注尾注、批注。原始 DOCX 的 material_id、MIME、内容 hash 和原文件继续保留。桥接对象按租户、会话、供应账号、原内容身份和投影版本隔离、持久化并校验。
- 含图片、嵌入对象、公式、修订、动态域或其他不能证明等价的结构时，拒绝文本桥接。仅当合同 metadata.native_material_mime_types 明确包含原生 DOCX MIME 时，才允许保留原二进制执行路径；否则在模型派发前报出 DOCX_PROJECTION_INELIGIBLE，不通过删去内容继续。
- 新桥接绑定可由现有 Gemini、Chat、Claude、Responses 和 Kimi 协议路径消费，仍受各自冻结的模态与协议限制。Gemini 创建缓存只使用内联表示，不把文件 URI 当作缓存对象来源。
- Gemini 60 MiB 筛选使用派生文本的实际字节，文本优先，其次小 PDF、大 PDF/原生 DOCX、视频和其他低密度材料。筛选不重排模型输入；退出缓存的 PDF、视频及其他附件完整留在正常推理分支。请求内嵌附件仍不得非法拆出缓存。

## 部署与验证

1. 数据库已经完成 009_cache_v66_patch.sql 时无需新迁移。更早实例仍须按既有迁移序列升级到 6.6.0 的数据库基线。
2. 暂停新请求受理并排空在途执行，再将 API、Worker 一并更新到 6.7.0；不要让旧 Worker 消费新冻结版本的请求。原有请求恢复继续使用原快照。
3. DOCX 功能按已发布供应合同启用；未授权供应保留原有能力约束。使用新 Session 验证新合同。
4. 本地验证覆盖纯逻辑、内存持久化、模拟 HTTP 上游、Grok 新旧载荷对照、旧版本计划/投影对照、Native 缓存与无缓存输入完整性、运行时单次派发及封存拒绝。检查程序仅在交付包之外运行，本包不新增 tests 文件。
5. 本次未调用真实 AIHubMix 或生产 Supabase；未验证生产命中率、账单、长时间 TTL 和完整数据库故障恢复。P3 计费心跳和后台资源清理仍未实施。
