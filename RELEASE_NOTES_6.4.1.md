# Model Relay 6.4.1 — 正常业务链内 Gemini 缓存诊断日志修补

以用户指定的 6.4.0 完整源码包为基线，完整复制后最小修改。此版补齐排障所缺的观测，ZZ 升级为 6.4.1；API/Worker 均需更新。不新增 SQL、依赖、tests 文件或额外上游调用。

## 请求诊断

新增 gemini_cache_request_diagnostics 事件，在 Adapter 完成模型参数、结构化输出和 CachedContent 禁用字段处理之后、首次发送之前生成。request_id/session_id 关联既有 request_accepted、材料投影及完成日志。

记录最终 payload、systemInstruction、contents、generationConfig、独立采样参数、thinkingConfig、Schema、输出 MIME、tools、toolConfig、safetySettings 的摘要；记录 cachedContent 是否存在及句柄摘要。完整正文、系统指令、Base64、URL、缓存句柄和密钥均不写入日志。

wire_content_items 按发送顺序提供每条 Content 的 hash、受控 role、已知 Part 类型计数，最多展示 32 项；wire_contents_hash 覆盖完整列表，wire_content_items_truncated 提示是否截断。诊断截断不修改实际发送内容。

material_projection_content_hash 单独识别物理计划里的稳定材料块；它与 wire_system_instruction_hash 分开。注意显式缓存请求只发送 cachedContent 句柄，此材料摘要是待复用的投影摘要，不声称正文又被发送。material_fingerprint 沿用 6.4.0 独立材料身份。记录现有布局、历史边界及冻结连接、Supply、Profile、账户摘要，不修改这些身份。

## 响应诊断

gemini_cache_usage_observed 事件的 request_facts / response_facts 携带请求和响应诊断；原有 hit 判定字段保留。

response_facts.observation_reason：
- provider_cached_tokens_positive：原生缓存 token 为正数。
- provider_zero_cached_tokens：原生缓存 token 明确为零。
- provider_cache_counter_missing：原生计数字段缺失。
- provider_cache_counter_invalid：字段存在，但类型/数值不合规范。

记录标准 usageMetadata 是否存在、类型、已知字段名、未知字段数量及合法整数 token。counter_observations 检查原生 camelCase、SDK snake_case、兼容接口 cached_tokens 等固定字段路径，只记录是否存在、类型及非负整数计数；不输出未知字段值。若计数仅出现在其他路径，alternate_counter_present/positive 提供排查线索，尚不绕过冻结协议合同或自动把该请求改判 hit。

响应头仅记录少量固定诊断头的摘要，不记录 Authorization 或任意头原文。头摘要变化只是关联线索，不证明缓存路由发生变化。

诊断随 result.observed.cache_diagnostics 返回，同时写入既有 cache_usage，并由 cache.gemini_diagnostics 对外提供。因此正常业务请求返回值和常规 API/Worker 日志都能提供证据，无需额外运行 probe。

## 如何用正常流程排查

沿同一 Session 的 request_id 对照：
1. material_reuse.reused > 0：确认 Relay 处理结果复用，不能当作 Provider KV 命中。
2. material_projection_content_hash 相同而 wire_system_instruction_hash 不同：材料相同，系统指令不同。
3. wire_sampling_hash / wire_schema_hash / wire_thinking_hash：分别定位采样、Schema、思考配置差异。
4. wire_content_items：看前部 Content 是否保持相同、差异从哪条开始；完整请求 hash 不同不等于公共前缀全部失效。
5. response_facts：确认缓存计数为正、为零、缺失、无效或出现在其他路径。

上述差异记录可定位下一步修改范围，不自动证明某一字段是 Provider 未命中的原因；参数是否参与缓存匹配取决于实际上游接口。当前原生 usage 字段缺失继续保持 unknown。

## 保持的执行边界

不修改业务输入、系统指令、材料排列、历史、Schema、模型参数、缓存策略、资源选择、经济性或派发规则。所有日志摘要均为旁路观察，不进入 ContextPlan/CacheSpec/执行绑定指纹，不成为 Provider 缓存 key。日志辅助计算失败按 fail-open 处理，不额外发送、不自动重试、不影响已有推理结果。6.4.0 材料复用逻辑及旧冻结请求语义继续保留。

## 验证与交付

现有 125 项测试通过；包外定向验证覆盖最终发送体摘要一致、字段独立变化、历史明细有界、计数缺失/零/正/异常及其他路径、正文与头部去敏、单次发送，以及诊断异常不改变请求体。API/Worker 导入及版本检查通过。基线 .git、隐藏文件和现有 tests 保持完整。

完整替换源码并同时重建 API 和 Worker。正常执行业务后，提供两项服务日志及响应即可继续排障。此版是内置诊断补丁，不宣称 Gemini 上游 hit 已修复；未进行生产计费调用。
