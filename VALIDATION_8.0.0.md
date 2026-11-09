# Model Relay 8.0.0 验证记录

本记录只计入本次实际执行结果。源码基线为用户上传的 7.1.0 原 ZIP；未调用真实 Google、AIHubMix 或 Moonshot 业务端点。验证代码及临时依赖在交付源码目录之外运行，不随包生成 tests 文件。

## 已通过

- Python 源码编译；137 个模块导入。
- API lifespan 启动/关闭及 Worker 初始化/关闭。使用占位存储地址与未配置的 Provider 凭据，仅验证装配及关闭路径，不执行数据库业务。
- 与原始 7.1.0 独立进程比较：内置 control_plane_hash 相同，RouteCatalog hash 相同，OpenAPI 完整结构除 info.version 从 7.1.0 到 8.0.0 外相同。
- 原工作簿离线解析：59 models、61 routes、61 caches，状态仍为 unverified_draft。
- 官方停用声明的 review 编译和认证输入命令通过；两者均不授予生产流量。
- 49 项受控检查通过，具体项目如下。

1. uncertified supply blocked
2. declaration-only candidate compiled
3. ascending channel order normalized
4. published pinned snapshot load
5. certification bound to exact contracts
6. unimplemented maintenance blocked
7. dynamic code reference blocked
8. arbitrary dual connections assembled
9. client pools isolated
10. account scopes isolated
11. same-connection SDK shared
12. route chooses highest qualified order
13. route freezes exact connection hash
14. unknown historical client blocked
15. files-only transport below threshold
16. external transport boundary inclusive
17. external transport above threshold
18. JSON projection unchanged
19. official profile emits no affinity
20. official inference native wire and auth
21. official unknown evidence audit preserved
22. required native evidence still rejects unknown
23. real SDK initialized with guarded transport
24. real SDK file URI cache create
25. revoked fence sends zero HTTP
26. all auth headers fully redacted
27. file URI cache bypasses inline budget
28. physical cache plan includes fileData
29. cache spec allows frozen file URI mode
30. active connection uses new endpoint
31. old frozen connection remains registered
32. old file and cache clients match old identity
33. concurrent stale release activation rejected
34. atomic release pointer advanced
35. pointer loads pinned approved release
36. result_stored recovery bypasses runtime
37. already dispatched recovery sends no inference
38. real SDK resumable upload uses configured connection
39. real SDK upload MIME preserved
40. revoked fence blocks SDK upload before HTTP
41. revoked fence blocks inference before HTTP
42. revoked fence blocks cache create before HTTP
43. inline-only cache contract rejects file references
44. explicit user content cannot be split for caching
45. non-Gemini channel compiles without Python changes
46. non-Gemini channel dispatch and response parsing
47. thinking budget projected from frozen contract
48. legacy material scope preserved
49. legacy frozen route resolves retained client

## 验证方法与范围

受控检查调用真实的 Connection/ControlPlane/Compiler、RouteResolver、Factory、Adapter、MaterialBinder 和 Gemini 物理投影代码。官方 SDK 检查实际初始化本地安装的 google-genai，通过 httpx 模拟底层 HTTP，验证 Native 推理、fileUri 缓存创建及 Resumable Upload 报文路径；没有发送真实 Provider 请求。认证记录和端点均为本地模拟，不随包发布为认证事实。

恢复检查验证 RequestExecutionService 对 result_stored 的提交旁路，以及对 dispatch_started 的再次执行拒绝；本次没有运行原生 PostgreSQL/Supabase、多连接竞争或真实跨节点故障注入，不能据此宣称生产故障恢复已全部验收。SQL 001–011 与原包逐字节相同；原版本的历史验证记录保留，仅供历史追踪。

修改提示词不要求生成 tests 文件。方案提到的“既有 68 项测试”未出现在上传源码包中，无法重跑原测试集合；本次独立检查不冒充该集合。

## 保留性检查

原包 166 个文件路径全部保留，未删除任何原文件。四个隐藏文件 .dockerignore、.env.example、.gitattributes、.gitignore 逐字节相同；原包没有 .git 目录，本次没有初始化或重建 .git。requirements.txt 和全部 SQL 文件逐字节相同。

## 尚需目标环境验收

真实渠道凭据、模型的具体 Thinking/Schema 支持、文件大小与 Token 上限、缓存命中与实际账单、响应证据可信性、文件上传中断恢复、原生数据库锁竞争、失租与跨节点分区。官方示例默认停用且未认证，必须通过独立认证和批准发布后才可获得流量。

本版不实现隐式前缀心跳保活、后台自动续期/孤儿清理或跨 Session 显式缓存共享，相关声明被准入校验阻断。新快照在进程启动时装载；本版不提供进程内热加载。旧快照自动保留引用而不自动退役，运维应按活跃会话/请求/pin 完成引用退出后再处理退役。

## 外部技术依据

- Google Gen AI SDK：https://googleapis.github.io/python-genai/
- Gemini Files API：https://ai.google.dev/gemini-api/docs/files
- Gemini Caching API：https://ai.google.dev/api/caching

文档说明不是具体 Supply 认证证据。
