# 文件上传机制增强补丁 V1.1 - 代码映射

| 补丁要求 | 0.4.0 实现 |
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
| DB delta | `sql/004_provider_native_file_ingress.sql` |

注意：补丁中引用的 `WF-NormalInference_20260910-NoComments.yml` 原文件未包含在本轮上传附件里；本实现按补丁文档已明确摘录的 V1.0.13 上传时序实现，`AIHUBMIX_GEMINI_BASE_URL` 必须配置为你现有工作流已验证的 Native Proxy base URL。
