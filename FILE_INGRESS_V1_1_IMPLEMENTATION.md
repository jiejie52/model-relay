# 文件上传机制增强补丁 V1.1 - 代码映射（0.5.0）

0.5.0 继续保留 0.4.0/0.4.1 的 Provider-native First 实现，但根据路由解耦方案把 **File Adapter 的选择权从调用方 target_connection_id 收回 Relay RouteResolver**。

| 补丁/路由要求 | 0.5.0 实现 |
|---|---|
| Gemini native-first | `app/materials/provider_files/gemini_aihubmix.py` |
| AIHubMix Native Proxy generateContent | `app/providers/gemini_native.py` |
| Kimi official Files API | `app/materials/provider_files/kimi_official.py` |
| Kimi text `/content` | `KimiOfficialFileAdapter._read_content()` + provider-derived extraction object |
| Kimi image/video `ms://` | `MoonshotChatAdapter._material_parts()` |
| Supabase input fallback only | `app/materials/fallback_storage.py` + MaterialIngress policy |
| Stable Material Registry | `relay_materials` + `provider_material_bindings` |
| account scope isolation | `Settings.connection_account_scope_hash()` + binding logical unique key |
| binding generation freeze | `BindingResolver.freeze_for_request()` + `relay_requests.material_binding_snapshot` |
| raw Files API Error | `ProviderHTTPError` headers/phase + MaterialIngress raw error detail |
| provider/model 自动选择 File Adapter | `app/routing/catalog.py` + `app/routing/route_resolver.py` |
| Material/Session/Inference 共用 route | `app/api_v2/router.py` + Session `_relay_route` metadata + Request snapshot |
| legacy target_connection 仅作 hint | `RouteResolver.handle_legacy_hint()` |
| source fetch / parse / policy / final failure 日志 | `app/materials/ingress.py` + `app/api_v2/router.py` |
| DB delta | 沿用 `sql/004_provider_native_file_ingress.sql`；0.5.0 无新增 migration |

## 0.5.0 Material 路由链

```text
caller: provider + model + material
        |
        v
RouteResolver
        |
        +-- private connection_id
        +-- route_revision
        +-- account_scope_hash
        |
        v
ProviderFileRegistry(connection_id)
        |
        +-- Gemini -> AIHubMix Gemini Native Files API
        +-- Kimi   -> Moonshot Official Files API
        `-- no native file adapter -> Relay fallback/bridge
```

Storage fallback 只改变原始字节保存方式，不能把 Gemini/Kimi 的推理 route 改成 generic Responses Adapter。

注意：文件上传机制增强补丁中引用的 `WF-NormalInference_20260910-NoComments.yml` 原文件不在此前 Relay 0.4.0 生成时的附件中；Gemini Files API 仍按补丁文档明确摘录的 V1.0.13 时序实现，`AIHUBMIX_GEMINI_BASE_URL` 必须配置为已验证的 Native Proxy base URL。
