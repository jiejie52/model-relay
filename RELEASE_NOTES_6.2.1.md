# Model Relay 6.2.1

## Gemini provider-managed implicit cache trial hotfix

This patch release is based on a complete copy of the uploaded 6.2.0 source package.

- Add `implicit_prefix` to the AIHubMix Gemini Native protocol profile without marking the Gemini family as verified.
- Keep the generic Gemini capability contract at `candidate`; allow a provider-managed implicit-prefix trial only for the exact `gemini-3.6-flash` model when both the frozen Supply contract and AIHubMix Gemini Native profile authorize it and the request uses Auto mode with an Auto mechanism preference.
- Do not allow `on` or an explicitly forced `implicit_prefix` to bypass the unverified contract.
- Treat the trial as Provider-managed: do not invent a minimum token threshold or require Relay-side `countTokens` admission; continue to require the complete Gemini layout/9 physical projection.
- Freeze an eligible implicit fallback before dispatch when explicit-resource economics or budget checks reject creation. Do not fall back after `create_unknown`, and never switch mechanisms or routes after `dispatch_started`.
- Classify Gemini `cachedContentTokenCount` as Hit (> 0), Miss (= 0), or Unknown (field absent). Do not infer a hit from Relay planning or `promptTokenCount`.
- Persist and log planned/effective mechanism, implicit-prefix fingerprint, payload hash, Provider cache token counts, and actual hit status.

No SQL migration, route expansion, layout/9 projection change, Session lifecycle change, instruction merging, or test-file generation.
