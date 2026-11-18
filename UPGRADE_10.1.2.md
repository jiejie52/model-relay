# Model Relay 10.1.2

## 问题与修复

10.1.1 的启动日志显示，Google Interactions 供给因探测 HTTP 400 被标记为 unavailable。高优先级供应配置为 reject_new_session 时，创建新 Session 因而返回 ROUTE_FALLBACK_FORBIDDEN。截图中的路由错误发生在业务推理之前，不代表 Google 拒绝了本次用户问题。

本版修复两个已证实的协议投影问题，并保留路由拒绝回退的设计。

1. Interactions 的 generation_config.thinking_level 发送小写档位。实际调用改用启动探测也使用的 project_thinking()，消除两套大小写逻辑。Relay 内部 AUTO/LOW/HIGH 统一意图枚举保持原值；Gemini generateContent 的 thinkingLevel 大写投影保持原值。
2. 业务请求和历史重放中的 fileData/inlineData 均通过同一个内容块投影器：图片、音频、视频分别使用 image/audio/video，其余材料使用 document；URI 对应 uri，内联字节对应 data，保留声明的 MIME 并去除 Content-Type 参数。修正原先不受 Interactions 支持的 type:file/type:inline_data。各模型对具体 MIME 的支持仍由上游能力决定。
3. Interactions 文件探测使用代码内生成的有效单页 PDF 和 application/pdf，经 Files API 上传后使用同一内容块投影器生成 document 输入，不再用通用 file 输入块。其他协议继续使用原有探测材料与流程。
4. 探测失败日志补充 model、offering_id，继续保留脱敏的 Google 错误正文，便于准确定位失败供给。
5. Interactions 验证版本升至 deployment-probes/gemini-interactions/3，触发相应供给重新验证；其他协议的验证版本不变。

## 思考参数与续接方案

未指定或留空思考深度时，现有解析器仍按 AUTO 解析；当前 adaptive 合同以 wire_action=omit 下发，不会新增 thinking_level。明确选择档位时，由冻结合同确定有效语义，再投影为本轮 generation_config。

thinking/think_level 仍不进入 parent_head_fingerprint 或当轮控制配置的历史前缀匹配；有效思考语义仍属于 Request 身份。切换档位不因参数本身破坏父锚点匹配。thought 与签名历史保真逻辑不变。启动探测选择代表性档位是能力核验，不是用户请求的默认档位。

## 部署与复测

1. API 和 Worker 一起部署本完整包，检查两个服务版本均为 10.1.2。无需新增依赖或 SQL 迁移。
2. 查看启动后的 workbook_configuration_loaded 中 google_gemini / gemini-3.1-flash-lite 是否真正 ready；如果仍为 pending/unavailable，按带模型标识的 deployment_probe_http_error 分析真实 Google 正文，不能把验证结果改写为 ready 或切换协议蒙混通过。
3. 使用最新 Postman 脚本：02 创建全新 Session，成功后再执行 03-1；保留 structured_output.mode=json_object，temperature 和 think_level 先留空。成功后再单独测试明确的思考档位。
4. 不要通过降低渠道优先级、放开回退或强行解锁旧 indeterminate Session 绕过这次问题。已派发且完成状态未知的旧请求仍不自动重发。

保留已有 JSON 响应格式修复、完整响应归档与解析、Session 隔离、冻结合同、单次派发和 result_stored 仅恢复提交。路由、工作簿配置、供应优先级、请求幂等算法和数据库脚本均未修改。

## 基线与验证边界

从用户上传的原始 model-relay-10.0.4.rar 全新完整解压，保留其全部 389 个文件与隐藏文件，再重放已经交付的响应处理补丁。本次版本变化属于 Bug 修复。没有初始化 Git，也未新增 tests 文件。

现有 68 项 unittest 与 3 项函数检查通过；127 项离线定向断言通过，包括实际调用与探测的小写档位、默认省略、原 generateContent 投影不变、文件 URI/内联字节的五类 MIME 映射、输入不变与封存校验、全部 6 个 Google 供给在严格 HTTP mock 下通过代表性探测，以及验证版本隔离。探测 PDF 另由 PDF 解析器验证为一页且文本可读。

这些是离线检查。本版尚未部署到用户环境，未使用真实 Google 凭据调用上游，也不保证实际服务一定 ready；必须以部署后的真实探测和业务请求结果为准。

## 官方依据

- https://ai.google.dev/api/interactions-api
- https://ai.google.dev/gemini-api/docs/file-input-methods
- https://ai.google.dev/gemini-api/docs/interactions-overview
