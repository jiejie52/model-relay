# Model Relay 10.10.0

本版本针对 Gemini（`gemini_interactions` 与 `gemini_native`）结构化输出下发原生 Schema 导致 Google 隐式前缀缓存未命中的问题，以及控制面冗余 `结构化输出` / `post_validate` 门控限制，完成全链路改造与治理对齐：

## 架构不变量调整摘要

1. **移除控制面结构化输出能力档位与 `post_validate` 运行期分支**：删除配置工作簿《模型清单》《能力合同》中的「结构化输出」列与编译期校验，解除 `post_validate` / `native_json_schema` 对准入过滤、执行受理与各协议适配器的限制；保留公共请求模型中的 `capability_requirements.structured_output` 字段作为向后兼容字段并在准入阶段静默放行。
2. **Gemini 全路径改为输入尾部 Schema 引导 + Relay 抽取校验包装**：`gemini_interactions` 与 `gemini_native`（包含单轮 `context_policy="explicit"` 分流路径）在 `json_schema` 模式下不再向 Google 下发 `response_format`、`responseSchema` 或 `responseMimeType`，保留 `system_instruction` 与前置历史前缀完全稳定，仅在当轮用户输入 Wire 尾部追加规范 Schema 约束提示；`ResultCommitter` 在保留完整原始响应审计归档的前提下，从返回文本中提取并按 Draft 2020-12 严格校验 JSON，再将规范 JSON 文本重新序列化写入 `result.text`。
3. **豁免 Gemini 提示词引导模式的上游原生约束证据强检**：因 Gemini 不再下发上游原生 Schema 约束，`result_contract` 与 `result_committer` 对 `gemini_interactions` / `gemini_native` 豁免 `UPSTREAM_NATIVE_CONSTRAINT_UNOBSERVED`、`UPSTREAM_NATIVE_CONSTRAINT_LOST` 与 `STRUCTURED_OUTPUT_NATIVE_CONSTRAINT_LOST` 核验。

## 变更文件

- [`app/config.py`](app/config.py) & [`app/control_plane/governance/workbook.py`](app/control_plane/governance/workbook.py)：`RELAY_VERSION` 升至 `10.10.0`，`COMPILER_VERSION` 升至 `relay-excel/10.10.0`；移除《能力合同》「结构化输出」列读取与校验，默认结构化保证标记设为 `relay_post_validate`。
- [`app/control_plane/governance/v2_import.py`](app/control_plane/governance/v2_import.py) & [`app/control_plane/governance/snapshot_compiler.py`](app/control_plane/governance/snapshot_compiler.py)：移除《模型清单》「结构化输出」列必填项及 `_STRUCTURED` 映射/能力匹配检查。
- [`app/control_plane/governance/deployment_validation.py`](app/control_plane/governance/deployment_validation.py)：静态验证版本升至 `static-contract/10.10.0` / `static-contract/gemini-interactions/10.10.0`，移除按 `native_json_schema` 的 facet 分支，为 Gemini 供应标记 `relay_post_validate`。
- [`app/control_plane/definitions.py`](app/control_plane/definitions.py) & [`app/compatibility/supply_seed.py`](app/compatibility/supply_seed.py)：`CapabilityContract.supports_requirements` 放行 `structured_output` 兼容请求字段；清理兼容种子中的 `post_validate` 标记。
- [`app/execution_plane/semantic_resolver.py`](app/execution_plane/semantic_resolver.py) & [`app/execution_plane/request_acceptor.py`](app/execution_plane/request_acceptor.py)：`validate_structured_output_capability` 不再按能力合同阻断请求；受理阶段仅校验请求 JSON Schema 语法，按有效协议与输出模式确定 `structured_output_guarantee`（Gemini `json_schema` 固定为 `relay_post_validate`）。
- [`app/canonical_schema.py`](app/canonical_schema.py) & [`app/execution_plane/structured_output.py`](app/execution_plane/structured_output.py)：新增 `format_structured_output_prompt_guidance`、`append_structured_output_prompt`、`extract_structured_json`（兼容 ```json 代码围栏及含内嵌代码块的 JSON / 前后提示文本）与 `format_structured_json_text`。
- [`app/protocol_adapters/wire_projection/gemini_interactions_wire.py`](app/protocol_adapters/wire_projection/gemini_interactions_wire.py) & [`app/protocol_adapters/profiles/gemini_native.py`](app/protocol_adapters/profiles/gemini_native.py)：停止发送 `response_format`、`responseSchema` 与 `responseMimeType`（`json_schema`），改为在当前轮次 Wire `input` / `contents[-1]` 尾部追加 `[OUTPUT_FORMAT_REQUIREMENT]` 提示，不污染本地 `user_content` 历史与材料位置校验摘要。
- [`app/protocol_adapters/profiles/chat_completions.py`](app/protocol_adapters/profiles/chat_completions.py) & [`app/protocol_adapters/profiles/claude_messages.py`](app/protocol_adapters/profiles/claude_messages.py)：解除 `post_validate` / `native_json_schema` 能力门控与降级分支，直接依据规范请求投影原生 JSON Schema 参数。
- [`app/execution_plane/result_contract.py`](app/execution_plane/result_contract.py) & [`app/execution_plane/result_committer.py`](app/execution_plane/result_committer.py)：对 `gemini_interactions` / `gemini_native` 豁免上游原生约束证据核验；提交阶段抽取并严格校验 JSON，再将规范 JSON 字符串回写 `compact_result.text`。
- [`config/model_management_v2.xlsx`](config/model_management_v2.xlsx)：删除《模型清单》「结构化输出」列并顺移`发布意向`/`本地检查`公式、数据验证与条件格式；删除《能力合同》「结构化输出」列并顺移`显式缓存材料模式`数据验证；将《协议配置》`gemini-official` 的「要求原生约束证据」设为「否」。
