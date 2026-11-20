import crypto from "node:crypto";
import {
  ContinuationDecisionError,
  IMPLEMENTED_CONTINUATION_PROTOCOLS,
  type ContinuationMode,
} from "./continuationContracts.js";
import {
  buildTurnPrefixChain,
  CONTRACT_DRIVEN_HEAD,
  continuationHead,
  validateJsonAgainstCanonicalSchema,
} from "./coreContinuation.js";
import { extractInputText } from "./providers.js";
import { stableHash, store } from "./store.js";
import type {
  ActualExecutionFact,
  CommittedContinuationAnchor,
  ContextPlan,
  ContinuationIntentPlan,
  EffectiveMaterialPlacementPlan,
  HistoryTurn,
  MaterialOccurrenceRef,
  MaterialRecord,
  ProviderMaterialBindingRecord,
  RequestRecord,
  SessionRecord,
} from "./types.js";

export function buildEffectiveMaterialPlacementPlan(params: {
  session: SessionRecord;
  visibleHistory: HistoryTurn[];
  modelHistoryVisible: boolean;
  effectivePolicy: "conversation" | "explicit" | "session_materials_only";
  explicitMaterialIds: string[];
  supportsNativeHistoryReuse: boolean;
}): EffectiveMaterialPlacementPlan {
  const sessionManifestAuto =
    params.effectivePolicy === "conversation" ||
    params.effectivePolicy === "session_materials_only"
      ? [...params.session.material_ids]
      : [];
  const requestExplicit = [...params.explicitMaterialIds];

  const historyCommittedSet = new Set<string>();
  if (params.modelHistoryVisible && params.supportsNativeHistoryReuse) {
    for (const turn of params.visibleHistory) {
      for (const mId of turn.material_ids ?? []) {
        historyCommittedSet.add(mId);
      }
      for (const occ of turn.material_occurrences ?? []) {
        historyCommittedSet.add(occ.material_id);
      }
    }
  }
  const historyCommitted = Array.from(historyCommittedSet);

  const explicitSet = new Set(requestExplicit);
  const historicalOnlyIds = sessionManifestAuto.filter(
    (id) => historyCommittedSet.has(id) && !explicitSet.has(id)
  );
  const historicalOnlySet = new Set(historicalOnlyIds);

  const currentEffectiveSet = new Set<string>();
  for (const id of sessionManifestAuto) {
    if (!historicalOnlySet.has(id)) {
      currentEffectiveSet.add(id);
    }
  }
  for (const id of requestExplicit) {
    currentEffectiveSet.add(id);
  }
  const currentEffectiveIds = Array.from(currentEffectiveSet);

  const digest = stableHash({
    version: "material-placement/2",
    session_manifest_auto: sessionManifestAuto,
    request_explicit: requestExplicit,
    history_committed: historyCommitted,
    historical_only_material_ids: historicalOnlyIds,
    current_effective_material_ids: currentEffectiveIds,
  });

  return {
    version: "material-placement/2",
    session_manifest_auto: sessionManifestAuto,
    request_explicit: requestExplicit,
    history_committed: historyCommitted,
    historical_only_material_ids: historicalOnlyIds,
    current_effective_material_ids: currentEffectiveIds,
    plan_digest: digest,
  };
}

export class ContextPlanner {
  static buildContextPlan(params: {
    session: SessionRecord;
    effectivePolicy: "conversation" | "explicit" | "session_materials_only";
    instructions: string | null;
    tools: unknown[];
    explicitMaterialIds: string[];
    providerPayload: Record<string, unknown>;
    fullHistory: HistoryTurn[];
    supportsNativeHistoryReuse: boolean;
  }): ContextPlan {
    const modelHistoryVisible =
      params.effectivePolicy === "conversation" &&
      params.session.context_policy === "conversation" &&
      params.session.history_mode !== "ephemeral_turn";

    const visibleHistory = modelHistoryVisible ? [...params.fullHistory] : [];

    const placementPlan = buildEffectiveMaterialPlacementPlan({
      session: params.session,
      visibleHistory,
      modelHistoryVisible,
      effectivePolicy: params.effectivePolicy,
      explicitMaterialIds: params.explicitMaterialIds,
      supportsNativeHistoryReuse: params.supportsNativeHistoryReuse,
    });

    const contract = params.session.route.continuation_contract;
    const headFormat = CONTRACT_DRIVEN_HEAD;
    const headInvariants = contract?.head_invariants ?? [
      "instructions",
      "tools",
      "account_scope_id",
      "model",
    ];

    const parentHeadFingerprint = continuationHead(
      {
        session_id: params.session.id,
        tenant_id: params.session.tenant_id,
        conversation_hash: params.session.conversation_hash,
        connection_id: params.session.route.connection_id,
        account_scope_id: params.session.route.account_scope_id,
        connection_spec_hash: params.session.route.connection_spec_hash,
        protocol_profile_hash: params.session.route.protocol_profile_hash,
        model: params.session.route.model,
        upstream_model: params.session.route.upstream_model,
        protocol: params.session.route.protocol,
        instructions: params.instructions,
        tools: params.tools,
        provider_payload: params.providerPayload,
      },
      { headFormat, headInvariants }
    );

    const turnPrefixChain = buildTurnPrefixChain(parentHeadFingerprint, visibleHistory);
    const reusableOnCommit =
      params.effectivePolicy === "conversation" &&
      params.session.context_policy === "conversation" &&
      params.session.history_mode === "cumulative";

    const planBody = {
      schema_version: "relay-context-plan/2" as const,
      model_history_visible: modelHistoryVisible,
      effective_policy: params.effectivePolicy,
      instructions: params.instructions,
      tools: params.tools,
      visible_history: visibleHistory,
      placement_plan: placementPlan,
      continuation_topology: {
        head_format: headFormat,
        parent_head_fingerprint: parentHeadFingerprint,
        visible_turn_count: visibleHistory.length,
        turn_prefix_chain: turnPrefixChain,
        reusable_on_commit: reusableOnCommit,
      },
    };

    return {
      ...planBody,
      context_plan_hash: stableHash(planBody),
    };
  }
}

export class ContinuationIntentPlanner {
  /**
   * Pure deterministic execution-plane planner (ZERO store/anchor ledger lookups).
   * Uses strict explicit certification allowlist + ZDR & require_stateful_anchor gates.
   */
  static planFrozenContext(params: {
    session: SessionRecord;
    contextPlan: ContextPlan;
    requestedContinuationMode?: string;
    requestedCacheMode: string;
    requestedCacheMechanism: string;
  }): ContinuationIntentPlan {
    const route = params.session.route;
    const contract = route.continuation_contract;
    const policy = route.continuation_policy;

    const topology = params.contextPlan.continuation_topology;
    const contractHash = route.continuation_contract_hash ?? "";
    const policyHash = route.continuation_policy_hash ?? null;

    const policyDefaultMode: ContinuationMode = policy?.default_mode ?? "off";
    let effectiveRequestedMode: ContinuationMode = policyDefaultMode;

    if (params.requestedContinuationMode) {
      const normalized = params.requestedContinuationMode.trim().toLowerCase();
      let mapped: ContinuationMode | null = null;
      if (normalized === "off" || normalized === "disabled") mapped = "off";
      else if (normalized === "auto" || normalized === "prefer_stateful_anchor") {
        mapped = "prefer_stateful_anchor";
      } else if (normalized === "require" || normalized === "require_stateful_anchor") {
        mapped = "require_stateful_anchor";
      } else {
        throw new ContinuationDecisionError(
          "INVALID_CONTINUATION_MODE",
          `Unsupported continuation_mode: ${params.requestedContinuationMode}`,
          422
        );
      }

      if (policy && !policy.allow_request_override && mapped !== policyDefaultMode) {
        throw new ContinuationDecisionError(
          "CONTINUATION_OVERRIDE_FORBIDDEN",
          "Continuation policy does not allow per-request continuation_mode override",
          422
        );
      }
      effectiveRequestedMode = mapped;
    }

    const baseDisabled = (reason: string): ContinuationIntentPlan => {
      const body = {
        schema_version: "relay-continuation-plan/2" as const,
        requested_mode: effectiveRequestedMode,
        planned_anchor_class: contract?.anchor_class ?? ("none" as const),
        resolution_status: "disabled" as const,
        decision_reason: reason,
        context_plan_hash: params.contextPlan.context_plan_hash,
        capability_contract_hash: contractHash,
        protocol_profile_hash: route.protocol_profile_hash,
        continuation_policy_hash: policyHash,
        model_history_visible: params.contextPlan.model_history_visible,
        reusable_on_commit: false,
        head_format: topology.head_format,
        parent_head_fingerprint: topology.parent_head_fingerprint,
        visible_turn_count: topology.visible_turn_count,
        turn_prefix_chain: topology.turn_prefix_chain,
        required_visible_material_ids:
          params.contextPlan.placement_plan.historical_only_material_ids,
        allow_multiturn_delta: false,
        allow_stateless_fallback: true,
        allow_head_mismatch_reopen: policy?.allow_head_mismatch_reopen ?? true,
        max_delta_turns: contract?.max_delta_turns ?? 0,
        anchor_safety_margin_seconds: contract?.anchor_safety_margin_seconds ?? 300,
      };
      return { ...body, plan_hash: stableHash(body) };
    };

    if (effectiveRequestedMode === "off") {
      return baseDisabled("continuation_mode_off");
    }

    // Strict explicit certified allowlist check (no blacklist default-true)
    const isCertified =
      Boolean(contract) &&
      contract!.anchor_class !== "none" &&
      contract!.verification_status === "verified" &&
      IMPLEMENTED_CONTINUATION_PROTOCOLS.has(route.protocol);

    if (!isCertified) {
      if (effectiveRequestedMode === "require_stateful_anchor") {
        throw new ContinuationDecisionError(
          "CONTINUATION_NOT_SUPPORTED",
          `Route offering ${route.offering_id} (${route.protocol}) is not certified for stateful continuation`,
          422
        );
      }
      return baseDisabled("protocol_or_contract_not_certified");
    }

    // ZDR Zero Data Retention compliance gate
    if (
      route.zero_data_retention &&
      contract!.requires_upstream_store &&
      contract!.forbid_when_zero_retention
    ) {
      if (effectiveRequestedMode === "require_stateful_anchor") {
        throw new ContinuationDecisionError(
          "CONTINUATION_FORBIDDEN_BY_ZDR_POLICY",
          "Zero Data Retention policy forbids upstream stateful continuation anchors",
          422
        );
      }
      return baseDisabled("zdr_forbidden");
    }

    // Mutually exclusive explicit cache conflict check
    if (
      params.requestedCacheMode === "on" &&
      contract!.mutually_exclusive_cache_mechanisms.includes(params.requestedCacheMechanism)
    ) {
      if (effectiveRequestedMode === "require_stateful_anchor") {
        throw new ContinuationDecisionError(
          "CONTINUATION_CACHE_CONFLICT",
          `Requested cache mechanism ${params.requestedCacheMechanism} conflicts with continuation contract`,
          422
        );
      }
      return baseDisabled("mutually_exclusive_cache_requested");
    }

    // Non-cumulative views (explicit / session_materials_only) are stateless and never touch or mint anchors
    if (!topology.reusable_on_commit) {
      return baseDisabled(`non_cumulative_context_policy_${params.contextPlan.effective_policy}`);
    }

    const body = {
      schema_version: "relay-continuation-plan/2" as const,
      requested_mode: effectiveRequestedMode,
      planned_anchor_class: contract!.anchor_class,
      resolution_status: "pending_runtime_bind" as const,
      decision_reason: "eligible_for_runtime_bind",
      context_plan_hash: params.contextPlan.context_plan_hash,
      capability_contract_hash: contractHash,
      protocol_profile_hash: route.protocol_profile_hash,
      continuation_policy_hash: policyHash,
      model_history_visible: params.contextPlan.model_history_visible,
      reusable_on_commit: topology.reusable_on_commit,
      head_format: topology.head_format,
      parent_head_fingerprint: topology.parent_head_fingerprint,
      visible_turn_count: topology.visible_turn_count,
      turn_prefix_chain: topology.turn_prefix_chain,
      required_visible_material_ids:
        params.contextPlan.placement_plan.historical_only_material_ids,
      allow_multiturn_delta: contract!.supports_multiturn_delta,
      allow_stateless_fallback: policy?.allow_stateless_fallback_pre_dispatch ?? true,
      allow_head_mismatch_reopen: policy?.allow_head_mismatch_reopen ?? true,
      max_delta_turns: contract!.max_delta_turns,
      anchor_safety_margin_seconds: contract!.anchor_safety_margin_seconds,
    };

    return {
      ...body,
      plan_hash: stableHash(body),
    };
  }
}

export class ResultCommitter {
  private static buildCommittedAnchor(params: {
    session: SessionRecord;
    request: RequestRecord;
    contextPlan: ContextPlan;
    continuationPlan: ContinuationIntentPlan;
    fact: ActualExecutionFact;
    newMaterialOccurrences: MaterialOccurrenceRef[];
    liveBindings: Map<string, ProviderMaterialBindingRecord>;
    newTurnChainDigest: string;
    newCoveredTurnCount: number;
  }): CommittedContinuationAnchor | null {
    const binding = params.fact.continuation_execution_binding;
    if (
      params.continuationPlan.schema_version !== "relay-continuation-plan/2" ||
      !params.contextPlan.continuation_topology.reusable_on_commit ||
      !binding.store_upstream_state ||
      params.contextPlan.effective_policy !== "conversation"
    ) {
      return null;
    }

    if (!params.fact.upstream_state_stored || !params.fact.upstream_anchor_ref) {
      return null;
    }

    if ((params.fact.previous_anchor_ref ?? null) !== (binding.upstream_handle ?? null)) {
      throw new Error(
        "UPSTREAM_CONTINUATION_HANDLE_MISMATCH: observed previous_anchor_ref disagrees with sealed binding"
      );
    }

    const contract = params.session.route.continuation_contract;
    if (!contract) return null;

    // Asymmetric TTL convergence: min(anchor retention TTL, min(covered material binding expires_at))
    const retentionMs = contract.retention_ttl_seconds * 1000;
    let earliestExpiryMs = Date.now() + retentionMs;

    const coveredMaterialsMap = new Map<string, MaterialOccurrenceRef>();
    for (const ref of binding.verified_anchor_material_bindings) {
      coveredMaterialsMap.set(`${ref.material_id}:${ref.history_entry_index}`, ref);
    }
    for (const turn of params.contextPlan.visible_history.slice(
      binding.delta_start_turn_index,
      binding.delta_end_turn_index
    )) {
      for (const ref of turn.material_occurrences ?? []) {
        coveredMaterialsMap.set(`${ref.material_id}:${ref.history_entry_index}`, ref);
      }
    }
    for (const ref of params.newMaterialOccurrences) {
      coveredMaterialsMap.set(`${ref.material_id}:${ref.history_entry_index}`, ref);
    }

    const coveredMaterialBindings = Array.from(coveredMaterialsMap.values()).sort(
      (a, b) => a.history_entry_index - b.history_entry_index
    );

    for (const ref of coveredMaterialBindings) {
      const live = params.liveBindings.get(ref.material_id);
      if (live) {
        const matExpMs = new Date(live.expires_at).getTime();
        if (Number.isFinite(matExpMs) && matExpMs < earliestExpiryMs) {
          earliestExpiryMs = matExpMs;
        }
      }
    }

    const nowIso = new Date().toISOString();
    return {
      id: crypto.randomUUID(),
      session_id: params.session.id,
      tenant_id: params.session.tenant_id,
      conversation_hash: params.session.conversation_hash,
      source_request_id: params.request.id,
      parent_anchor_id: binding.selected_anchor_id,
      connection_id: params.session.route.connection_id,
      account_scope_id: params.session.route.account_scope_id,
      account_scope_hash: params.session.route.account_scope_hash,
      connection_spec_hash: params.session.route.connection_spec_hash,
      protocol: params.session.route.protocol,
      model: params.session.route.model,
      anchor_class: binding.anchor_class,
      upstream_handle: params.fact.upstream_anchor_ref,
      affinity_epoch: params.fact.affinity_epoch,
      history_version: params.request.expected_history_version + 1,
      is_root_prefix: true,
      covered_turn_count: params.newCoveredTurnCount,
      parent_head_fingerprint: params.continuationPlan.parent_head_fingerprint,
      turn_chain_digest: params.newTurnChainDigest,
      covered_material_bindings: coveredMaterialBindings,
      covered_materials_digest: stableHash(coveredMaterialBindings),
      status: "active",
      expires_at: new Date(earliestExpiryMs).toISOString(),
      invalidated_by_request_id: null,
      invalidation_reason: null,
      created_at: nowIso,
      updated_at: nowIso,
    };
  }

  private static validatePostExecutionGates(params: {
    structuredOutput: Record<string, unknown>;
    fact: ActualExecutionFact;
  }): { ok: true; guarantee: string } | { ok: false; code: string; message: string } {
    const mode = String(params.structuredOutput.mode || "none").toLowerCase();
    if (
      mode === "json_schema" &&
      params.structuredOutput.schema &&
      typeof params.structuredOutput.schema === "object"
    ) {
      const check = validateJsonAgainstCanonicalSchema(
        params.fact.text,
        params.structuredOutput.schema as Record<string, unknown>
      );
      if (!check.ok) {
        return {
          ok: false,
          code: "CANONICAL_SCHEMA_POST_VALIDATION_FAILED",
          message: check.reason,
        };
      }
      return { ok: true, guarantee: "native_json_schema" };
    }
    if (mode === "json_object" || mode === "object") {
      // UPGRADE_10.1.4: Relay wraps raw text as relay_result_object without requiring JSON parsing
      return { ok: true, guarantee: "relay_result_object" };
    }
    return { ok: true, guarantee: "none" };
  }

  static finalize(params: {
    session: SessionRecord;
    request: RequestRecord;
    contextPlan: ContextPlan;
    continuationPlan: ContinuationIntentPlan;
    fact: ActualExecutionFact;
    input: unknown;
    currentMaterials: MaterialRecord[];
    liveBindings: Map<string, ProviderMaterialBindingRecord>;
    structuredOutput: Record<string, unknown>;
    cacheMode: string;
    cacheMechanism: string;
  }): { ok: boolean } {
    const gate = ResultCommitter.validatePostExecutionGates({
      structuredOutput: params.structuredOutput,
      fact: params.fact,
    });
    if (!gate.ok) {
      params.request.error = {
        source: "relay",
        code: gate.code,
        message: gate.message,
      };
      store.releaseContinuationAfterRequest({
        session: params.session,
        request: params.request,
        invalidateAnchorId: null,
        invalidationReason: null,
      });
      return { ok: false };
    }

    const nowIso = new Date().toISOString();
    const userText = extractInputText(params.input);
    const existingHistory = store.getHistory(params.session.id);
    const nextUserIndex = existingHistory.length;
    const nextModelIndex = existingHistory.length + 1;

    const newMaterialOccurrences: MaterialOccurrenceRef[] = params.currentMaterials.map((mat) => {
      const live = params.liveBindings.get(mat.id);
      return {
        material_id: mat.id,
        history_entry_index: nextUserIndex,
        binding_generation: live?.binding_generation ?? 1,
        provider_binding_id: live?.provider_binding_id ?? null,
        external_uri: live?.external_uri ?? null,
        representation: live?.representation ?? "inline_or_text",
        part_digest: mat.sha256,
      };
    });

    const newTurns: HistoryTurn[] =
      params.session.context_policy === "conversation" &&
      params.session.history_mode === "cumulative"
        ? [
            {
              role: "user",
              text: userText,
              material_ids: params.currentMaterials.map((m) => m.id),
              material_occurrences: newMaterialOccurrences,
              request_id: params.request.id,
              timestamp: nowIso,
            },
            {
              role: "model",
              text: params.fact.text,
              native_steps: params.fact.native_steps,
              request_id: params.request.id,
              timestamp: nowIso,
            },
          ]
        : [];

    // Compute updated chain digest covering visible_history + newly committed user/model turns
    let newTurnChainDigest = "";
    let newCoveredTurnCount = params.contextPlan.continuation_topology.visible_turn_count;
    if (newTurns.length > 0) {
      const updatedChain = buildTurnPrefixChain(
        params.continuationPlan.parent_head_fingerprint,
        [...params.contextPlan.visible_history, ...newTurns]
      );
      newTurnChainDigest = updatedChain[updatedChain.length - 1]?.turn_chain_digest ?? "";
      newCoveredTurnCount = updatedChain.length;
    }

    const anchorCandidate = ResultCommitter.buildCommittedAnchor({
      session: params.session,
      request: params.request,
      contextPlan: params.contextPlan,
      continuationPlan: params.continuationPlan,
      fact: params.fact,
      newMaterialOccurrences,
      liveBindings: params.liveBindings,
      newTurnChainDigest,
      newCoveredTurnCount,
    });

    const binding = params.fact.continuation_execution_binding;
    const cachedTokens = params.fact.usage.cached_tokens;
    const promptTokens = params.fact.usage.prompt_tokens;

    const cacheSummary = {
      requested_cache_mode: params.cacheMode,
      requested_cache_mechanism: params.cacheMechanism,
      planned_mechanism:
        params.cacheMode === "off"
          ? "none"
          : params.session.route.protocol === "gemini_interactions"
            ? "implicit_prefix"
            : params.cacheMechanism,
      effective_cache_mechanism:
        cachedTokens && cachedTokens > 0 ? "implicit_prefix" : "none",
      cache_read_tokens: cachedTokens,
      uncached_input_tokens:
        promptTokens !== null && cachedTokens !== null
          ? Math.max(0, promptTokens - cachedTokens)
          : promptTokens,
      actual_cache_hit_status: cachedTokens && cachedTokens > 0 ? "hit" : "unknown",
    };

    const continuationSummary =
      params.continuationPlan.resolution_status === "pending_runtime_bind" ||
      binding.effective_mode !== "disabled"
        ? {
            schema_version: "relay-continuation-summary/2",
            continuation_mode: params.continuationPlan.requested_mode,
            effective_mode: binding.effective_mode,
            anchor_class: binding.anchor_class,
            mechanism:
              binding.effective_mode === "stateful_anchor_delta"
                ? (params.session.route.protocol_profile_continuation?.request_anchor_field ??
                    "previous_interaction_id")
                : "stateless_replay",
            active_anchor_id: anchorCandidate?.id ?? null,
            parent_anchor_id: binding.selected_anchor_id,
            covered_turn_count: binding.covered_turn_count,
            delta_turn_range: [binding.delta_start_turn_index, binding.delta_end_turn_index],
            bound_turns_count:
              newCoveredTurnCount || params.session.history_version + (newTurns.length > 0 ? 1 : 0),
            decision_reason: binding.decision_reason,
            rejected_candidates_audit: binding.rejected_candidates_audit,
            continuation_usage: {
              relay_omitted_history_estimated_tokens:
                params.fact.usage.omitted_history_estimated_tokens,
              upstream_total_input_tokens: promptTokens,
              upstream_implicit_cached_tokens: cachedTokens,
            },
          }
        : null;

    const resultEnvelope: Record<string, unknown> = {
      schema_version: "relay-result/2.1",
      request_id: params.request.id,
      session_id: params.session.id,
      provider: params.session.provider,
      model: params.fact.model,
      structured_output_guarantee: gate.guarantee,
      text: params.fact.text,
      usage: params.fact.usage,
      cache: cacheSummary,
      ...(continuationSummary ? { _relay_continuation: continuationSummary } : {}),
    };

    store.completeContinuationContext({
      session: params.session,
      request: params.request,
      newTurns,
      materialOccurrences: newMaterialOccurrences,
      anchorCandidate,
      resultEnvelope,
      cacheSummary,
      continuationSummary,
    });

    // Record model turn index reference cleanly for traceability
    void nextModelIndex;
    return { ok: true };
  }

  /**
   * Commit-only recovery path for requests already archived at provider_dispatch_state = 'result_stored'.
   * Enforces sealed continuation binding & payload hash consistency across ALL v2 continuation protocols.
   */
  static recoverAndCommit(params: {
    session: SessionRecord;
    request: RequestRecord;
    contextPlan: ContextPlan;
    continuationPlan: ContinuationIntentPlan;
    input: unknown;
    currentMaterials: MaterialRecord[];
    liveBindings: Map<string, ProviderMaterialBindingRecord>;
    structuredOutput: Record<string, unknown>;
    cacheMode: string;
    cacheMechanism: string;
  }): { ok: boolean } {
    if (params.request.provider_dispatch_state === "committed") {
      return { ok: true };
    }
    if (params.request.provider_dispatch_state !== "result_stored") {
      throw new Error(
        `recoverAndCommit requires result_stored state, got ${params.request.provider_dispatch_state}`
      );
    }

    const fact = store.executionFacts.get(params.request.id);
    if (!fact) {
      throw new Error(`Missing archived execution fact for result_stored request ${params.request.id}`);
    }

    // Enforce sealed continuation consistency across ALL v2 continuation protocols (no protocol hardcoding)
    if (
      params.continuationPlan.schema_version === "relay-continuation-plan/2" &&
      fact.continuation_execution_binding.effective_mode !== "disabled"
    ) {
      if (
        !params.request.sealed_continuation_binding ||
        params.request.sealed_continuation_binding.binding_hash !==
          fact.continuation_execution_binding.binding_hash
      ) {
        throw new Error(
          "CONTINUATION_RECOVERY_BINDING_MISMATCH: execution fact disagrees with sealed_continuation_binding"
        );
      }
      if (params.request.prepared_payload_hash !== fact.seal_hash) {
        throw new Error(
          "CONTINUATION_RECOVERY_SEAL_MISMATCH: execution fact disagrees with prepared_payload_hash"
        );
      }
    }

    return ResultCommitter.finalize({
      session: params.session,
      request: params.request,
      contextPlan: params.contextPlan,
      continuationPlan: params.continuationPlan,
      fact,
      input: params.input,
      currentMaterials: params.currentMaterials,
      liveBindings: params.liveBindings,
      structuredOutput: params.structuredOutput,
      cacheMode: params.cacheMode,
      cacheMechanism: params.cacheMechanism,
    });
  }
}
