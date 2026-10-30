# Model Relay V2 workbook as governed runtime configuration

**Authoring input:** `config/model_management_v2.xlsx` (the original V2
four-worksheet contract, now expanded to a 2026-10-28 mainstream-model
candidate catalog (supplemented with user-supplied upstream IDs)). It is NOT a test fixture. The source file is included in
the application Docker image. The expanded workbook has **59 model rows, 61
channel candidate rows and 61 cache rows**, with a 100-row editable table
capacity in each governance sheet. Excel local-check formulas, validations,
filterable tables and provider-reference links cover the expanded range.

**Every newly added model is unpublished and has no enabled route or cache.**
An entry documents only the proposed public model name, candidate provider,
upstream identifier and conservative desired capabilities (including an
8,192/16,384 Token *target*, not a verified provider maximum). Some identifiers
are provider-specific aliases; Meta Llama entries carry a repository ID, not an
API endpoint model ID. Sources are in the worksheet `填写说明` and cell comments.
The actual account, region, protocol, limits, upstream alias and cache behavior
must be certified for each supply before activation. These new provider labels
are recognized by the *draft compiler*, not added to the live Adapter registry.

## 2026-10-28 model-ID audit supplement

The current 59-model V2 workbook includes all **18 distinct model IDs** in
both the user-provided provider selector screenshot (9) and supplementary
text list (9): eight were already present, ten are new. Four additional
important official model IDs were also added: `gpt-6.1-sol`,
`claude-opus-5`, `mimo-v2.6-pro`, `glm-5.3-flash`.

The ten newly supplied missing IDs are: `gemini-3.1-pro-preview`,
`gemini-3.1-pro-preview-search`, `gemini-3.5-flash-lite-free`,
`grok-4.5`, `grok-4.6`, `kimi-k3`, `kimi-k2.7-code`,
`claude-sonnet-5`, `coding-glm-5.3-free`, and
`xiaomi-mimo-v2.6-pro-free`.

**Do not conflate model producer and API route.** The literal `-free` and
`-search` suffixes are kept exactly as supplied, but are unverified candidate
*aggregator route IDs*, not confirmed official vendor model IDs. The workbook
uses separate proposed channel labels and highlights these uncertainties in
`填写说明` / `ModelIdReview` and cell notes. For instance,
`mimo-v2.6-pro` is Xiaomi's official API model ID; the separately supplied
`xiaomi-mimo-v2.6-pro-free` is a tentative aggregator-only route. Do not
silently resolve one to the other. Every added model is unpublished with a
disabled route and disabled Relay cache policy.

This supplement also corrects the old GLM source link in `填写说明` from an
unrelated vendor to Z.AI. Sources reference official vendor documentation when
available; image-only free/search aliases remain user-provided observations,
not independent vendor certification. The declaration compiler recognizes the
Xiaomi provider label as a draft, but this does not add an execution Adapter.

**Runtime input:** a separate, externally approved and SHA256-pinned, immutable
`MODEL_CONTROL_PLANE_FILE` JSON release. API and Worker both load this file
through `ModelControlPlane.from_settings()` at startup and freeze the chosen
Route/Capability/Cache contract into each new Session. Excel is never read in
the request execution path, as required by the V5 plan.

All included rows are disabled / not submitted for publication.
**Do not assume any listed models or channels are certified.**
Changing cells by itself does not grant traffic; it produces new declarations
for an operator to review and release. Do not publish AIHubMix/Google routes
until certified on the actual model, account, endpoint and protocol.

## One-time deployment steps

1. Set the normal Relay service environment variables (the compiler uses the
   existing backend control-plane registry without contacting a provider).
2. Validate the workbook:

   ```sh
   python -m app.governance.cli validate --workbook config/model_management_v2.xlsx
   ```

3. Provide a **server-owned** `deployment-map.json` mapping workbook environments
   (for example `"测试"`) to each application's actual `DEPLOYMENT_ID`. Do not
   guess that `"测试"` always means Railway. For routes proposed for activation,
   also supply a server-owned `supply-registry.json` with schema
   `relay-v2-supply-registry/1`, and a `supplies` array. Each supply specifies
   `model`, `environment`, `channel`, `upstream_model`, `offering_id`,
   `certification_status: "passed"`, a nonempty `evidence_ref`, an externally
   authorized `budget_authorization_ref`, and `validated_max_output_tokens`
   covering the workbook's target output limit. The offering
   must already be exactly registered in the server's control plane with a
   matching model, provider, deployment, capability, protocol, and cache
   mechanism. This sidecar is **not** populated from Excel. Physical provider
   certification and independent review remain outside this CLI.

4. Compile a **non-executable** review candidate:

   ```sh
   python -m app.governance.cli candidate \
     --deployment-map /secure/deployment-map.json \
     --supply-registry /secure/supply-registry.json \
     --revision relay-v2-governance/release-001 \
     --out config/candidates/release-001.review.json
   ```

   For a disabled-only sample, `--supply-registry` can be omitted; it will NOT
   make any unregistered supplier executable. The output has `status=review`
   and runtime will reject it.

5. Obtain a separate, authorized operator approval of the exact candidate and
   a change preview against the current deployed snapshot. In the controlled
   deployment workflow, prepare an `approval.json` such as:

   ```json
   {
     "candidate_sha256": "<sha256 of canonical candidate JSON>",
     "base_control_plane_hash": "<base hash from candidate release_metadata>",
     "approved_by": "<trusted release operator>",
     "approval_ref": "<approval/change ticket ID>"
   }
   ```

   The `candidate_sha256` is the hash of compact sorted-key canonical JSON
   (see `app.governance.v2_runtime.digest`, not the pretty-printed file hash).
   **This CLI verifies matching metadata but cannot authenticate human
   approvers.** Restrict deployment/approval access and manage evidence in your
   existing release-control system. Do not treat an arbitrary text file as
   proof of real provider certification.

6. After approval, build the published file (never overwrite an existing
   release file):

   ```sh
   python -m app.governance.cli publish \
     --candidate config/candidates/release-001.review.json \
     --approval /secure/approval.json \
     --out config/runtime/release-001.published.json
   sha256sum config/runtime/release-001.published.json
   ```

7. Point both API and Worker to the **same versioned read-only file**, with the
   SHA256 of its exact bytes pinned:

   ```sh
   MODEL_CONTROL_PLANE_FILE=config/runtime/release-001.published.json
   MODEL_CONTROL_PLANE_FILE_SHA256=<exact sha256sum from previous step>
   ```

   Deploy both services with the same artifact. Startup fails if the file is
   missing, modified, not published, or compiled for a different built-in
   control-plane baseline. `MODEL_CONTROL_PLANE_JSON` and the file override
   cannot be used together. `ROUTE_CATALOG_JSON` cannot override any published
   V2 managed group. A new release is loaded at process startup; editing Excel
   or publishing a new file never rewrites existing Session bindings.

## Current boundaries

- This is **offline compiler + immutable file runtime loading**, not a live
  spreadsheet watcher, dynamic database activation, RBAC or an approval UI.
- The compiler accepts an enabled route only when it can match an exact,
  certified offering in the current server registry. No supplied Excel row
  can create a new Provider Adapter/Protocol Profile/Capability Contract.
- **Enabled V2 cache policies are deliberately publication-blocked** until
  session-frozen defaults, force authorization and idempotency integration
  are implemented. Template cache rows remain declarations, not live runtime
  policy until that code exists.
- Omitting an existing production row is not a deletion instruction. The
  initial compiler targets a controlled first release; incremental changes
  to an already published V2 catalog need a baseline comparison/migration
  workflow before publication.
- Gemini 3.6 Flash/4 Argon still require actual account/endpoint certification.
