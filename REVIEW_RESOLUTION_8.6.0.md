# 代码评审实施对应

评审基线：8.5.0；本版：8.6.0。依据实际执行链补齐，不以“文件是否改过”代替行为验收，也不声明百分比生产认证。

| 评审缺口 | 实施位置与行为 | 验证 |
| --- | --- | --- |
| 缓存预算 | control_plane/cache_contracts.py 统一模式解析和跨合同验证；runtime/cache_contracts.py 固化模式；cache_selector.py 按实际表示收费；material_binder.py 使原有 fileUri 分支进入统一预算选择器 | 80 MiB URI 进入实际缓存投影；同大小 inline 仍拒绝；外链无豁免；长 URI 按真实引用开销收费 |
| 原生响应体证据 | execution_authorization.py、native_evidence.py、Gemini Native Adapter、result_contract.py；Excel 配置独立证据模式及强制要求 | 原生请求/响应正常时通过结果合同；截断、修复、无模型、无 Schema、负面证据拒绝；直连来源不能冒充免费账单 |
| 材料重绑资格 | material_reachability.py、acceptance_support.py、admission.py | 不同账号、过期和错误传输类型不合格；无目标绑定及保留字节在 Session 创建前拒绝；复用 8.5.0 的允许/禁止回退检查 |
| 上传和脱敏 | transport_guard.py 包装流并监听失租；Files Adapter 与 SDK 完成后复核；http_wire_logger.py 统一清洗 | 实际 SDK start / finalize 通过同一围栏；阶段间失租零 finalize；流中失租阻断后续块；阻塞发送取消；URL 查询秘密不可见 |
| 强类型指纹及编译阻断 | 共享连接契约、FrozenExecutionPlan、SealedDispatchBundle、RouteBinding、Request、PlanLoader、ConditionVerifier、DispatchSealer、ScopedAdapter；基础控制面与 snapshot_compiler 共用静态校验 | 新旧封存兼容、字段缺失/篡改/未知版本拒绝、实际适配器复核；缺 Files 或文件适配器的缓存合同拒绝 |

8.5.0 已有的 fileUri 专用路径、跨作用域无 fallback 拒绝和日志移除 Query 继续保留并补强，未将这些原有防护误判为完全缺失。

明确的验收边界：响应体证据不是上游签名证明，official_direct 不是实际金额；source_recoverable 标签不等于源可读；取消传输不等于撤销远端副作用；原始文件超过本地 inline 预算不等于上游允许该文件。真实账号可用性、网络行为和费用仍需部署验证。
