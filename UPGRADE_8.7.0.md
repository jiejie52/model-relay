# Model Relay 8.7.0 思考能力映射升级说明

基线为用户提供的 model-relay-8.6.0.zip。此次属于既有思考解析处理方式增强，版本由 8.6.0 升至 8.7.0。源码包保留全部原有文件，只修改本次链路所需模块，不附加测试文件。

## 实施内容

公共请求新增 `think_level_policy=prefer|require`，缺省为 prefer。思考意图统一为 AUTO、OFF、LOW、MEDIUM、HIGH、XHIGH、MAX；省略、显式空值和显式 AUTO 分别记录。none、disabled、false 字符串作为受控 OFF 别名。未知值拒绝。

执行面先查询冻结合同的映射，再检查是否允许及是否满足硬约束，生成带版本的 `thinking` 节点。协议适配器只投影该节点的 effective 内容。原始标签、来源和规则说明保留用于审计；实际控制模式、动作、策略、档位、预算和约束策略参与请求身份。

思考激活时按合同检查 temperature/top_p 和预算与最大输出的关系。禁止的默认采样值不自动补写。Gemini、Responses、Chat Completions、Claude Messages、Moonshot 共用注册式投影；预算、开关、离散档位和自适应配置由合同确定。

思考统计、内容块存在性及受控响应头进入 `observed_thinking`，随原始响应写入 execution-fact.json。ResultCommitter 在正常完成与只提交恢复时核验剥离、降级和关闭状态矛盾。确定性违约保留原始 2xx 响应引用并失败，不再次推理。未上报的统计保持 unknown，不按零或关闭处理，Token 数不被当作深度证明。

## Excel 配置

使用本包的 config/model_management_v2.xlsx。删除模型清单的思考档位列，以及能力合同中的重复白名单与分散预算列；新增思考映射清单。6 份现有合同各有 7 条规则，共 42 条。原有其他工作表内容、原生对象与输入验证保留，受移动列影响的检查公式、条件格式和验证范围同步调整。

仅维护思考映射清单中的请求到有效语义关系。AUTO 必须允许；OFF 不得映射成开启；固定开启不自动满足 HIGH/MAX 等深度硬要求。普通超档偏好按表中规则处理，运行时不猜测相近档位。编译器生成供应能力摘要，Session 的 think_levels 资格要求使用硬支持集合。

能力合同增加可选约束列：显式 AUTO 是否省略参数、思考激活禁用温度/TopP、固定温度、预算上下限、预算小于输出上限。布尔值可填 TRUE/FALSE 或 是/否。协议配置支持追加思考参数剥离、思考降级及有效思考模式响应头列；只采信已配置头，不假设供应商必然提供某个头。

日常操作延续修改 Excel、配置凭据、重新部署，由系统自动读取、校验、编译、保存和加载快照。无需手工维护发布 JSON 或激活指针。旧版工作簿不包含新映射表，不能直接作为本版配置输入。

## 冻结和恢复边界

Session 的供应、账号作用域、连接指纹与合同继续冻结。新发布的映射配置不覆盖旧 Session。对旧格式合同，新请求通过内存兼容转换读取原有映射；不支持的普通深度偏好只可收敛至旧合同已声明的 AUTO 执行，标记不满足 require，OFF 不使用该回退，不新增原生能力。转换不写回旧快照。

已经受理的旧 Request 不重新解析或补入新 thinking 节点，保留原解析、原生投影和四种历史身份算法。新的 thinking 节点使用独立版本门禁，避免以新算法比较旧请求。

缓存内容身份与本轮思考控制分离。真实 Gemini layout/9 规划验证了同一冻结内容切换 LOW/HIGH 不改变缓存内容及兼容历史前缀指纹。完整报文字节诊断仍如实记录参数变化。历史增长、资源过期和供应限制仍影响实际复用，不承诺每次必然命中。cachedContents 创建边界拒绝单轮 thinking/generation 配置。

not_sent、dispatch_started、result_stored 的既有恢复语义、ExecutionFence、TransportGuard 与原子提交机制保留。进入 dispatch_started 后不因本次思考核验而换渠道、换机制或重新调用模型。API 与 Worker 应一同部署本版。

## 验证记录与边界

通过 Python 编译、API/Worker 导入、工作簿离线编译，以及随包修改清单记录的 21 组本地验证。验证覆盖实际 RequestAcceptor 新建及幂等重放、五类投影、六种模式、配置阻断、旧身份对照、真实缓存规划函数、实际 ControlledInvoker 归档路径和 ResultCommitter 只提交恢复。上游、对象存储和数据库使用受控替身；未进行真实账号、数据库 RPC、网络、计费或生产端到端认证，不将历史文档中的 68 项测试结果计为本次结果。

原始包未附 requirements.txt，而 Dockerfile 引用了该文件。本次没有代填未知依赖或改动 Dockerfile。部署时需沿用现有环境中匹配的依赖清单；仅使用此包重新构建 Docker 镜像，仍会受到该原包缺件影响。本次不需要新增 SQL 迁移。
