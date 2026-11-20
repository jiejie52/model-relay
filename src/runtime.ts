import { verifyParentChainMaterialCoverage } from "./coreContinuation.js";
import { stableHash, store } from "./store.js";
import type {
  CommittedContinuationAnchor,
  ContextPlan,
  ContinuationExecutionBinding,
  ContinuationIntentPlan,
  MaterialOccurrenceRef,
  MaterialRecord,
  ProviderMaterialBindingRecord,
  RejectedCandidateAudit,
  RequestRecord,
  SealedDispatchBundle,
  SessionRecord,
} from "./types.js";

export interface RuntimeExecutionState {
  session: SessionRecord;
  request: RequestRecord;
  contextPlan: ContextPlan;
  continuationPlan: ContinuationIntentPlan;
  currentMaterials: MaterialRecord[];
  liveBindings: Map<string, ProviderMaterialBindingRecord>;
  historicalPinnedBindings: MaterialOccurrenceRef[];
  continuationBinding: ContinuationExecutionBinding | null;
  preparedPayloadDigest: string;
  sealedBundle: SealedDispatchBundle | null;
  wireAffinityEpoch: number | null;
}

export class MaterialBinder {
  /**
   * Runs strictly BEFORE ContinuationBinder so all active material bindings
   * and binding_generation records are verified prior to anchor matching.
   */
  static bindForPlan(state: RuntimeExecutionState): void {
    const accountScopeId = state.session.route.account_scope_id;
    const connectionId = state.session.route.connection_id;

    // Bind materials needed in the current turn AND any materials referenced in historical ledger
    const allSessionMatIds = new Set<string>([
      ...state.currentMaterials.map((m) => m.id),
      ...state.contextPlan.placement_plan.historical_only_material_ids,
    ]);

    for (const matId of allSessionMatIds) {
      const mat = store.materials.get(matId);
      if (mat) {
        const live = store.getOrCreateMaterialBinding(mat, accountScopeId, connectionId);
        state.liveBindings.set(mat.id, live);
      }
    }

    // Collect historical pinned binding references
    const pinned: MaterialOccurrenceRef[] = [];
    for (let i = 0; i < state.contextPlan.visible_history.length; i++) {
      const turn = state.contextPlan.visible_history[i];
      for (const occ of turn.material_occurrences ?? []) {
        pinned.push(occ);
      }
    }
    state.historicalPinnedBindings = pinned;
  }
}

export class ContinuationBinder {
  private static isAnchorScopeValid(
    candidate: CommittedContinuationAnchor,
    session: SessionRecord,
    safetyMarginSeconds: number
  ): boolean {
    if (candidate.status !== "active") return false;
    if (candidate.session_id !== session.id) return false;
    if (candidate.tenant_id !== session.tenant_id) return false;
    if (candidate.conversation_hash !== session.conversation_hash) return false;
    if (candidate.connection_id !== session.route.connection_id) return false;
    if (candidate.connection_spec_hash !== session.route.connection_spec_hash) return false;
    if (candidate.account_scope_hash !== session.route.account_scope_hash) return false;
    if (candidate.model !== session.route.model) return false;

    const expiresMs = new Date(candidate.expires_at).getTime();
    if (!Number.isFinite(expiresMs) || expiresMs <= Date.now() + safetyMarginSeconds * 1000) {
      return false;
    }
    return true;
  }

  private static makeBinding(
    params: Omit<ContinuationExecutionBinding, "binding_version" | "binding_hash">
  ): ContinuationExecutionBinding {
    const body = {
      binding_version: "relay-continuation-binding/2" as const,
      ...params,
    };
    return {
      ...body,
      binding_hash: stableHash(body),
    };
  }

  /**
   * Binds an eligible parent anchor using True Prefix DAG matching, three anchor_class policies,
   * affinity_epoch checks, binding_generation matching, and parent-chain material coverage proofs.
   */
  static bindForContext(
    state: RuntimeExecutionState,
    options?: { reopen?: boolean }
  ): ContinuationExecutionBinding {
    const plan = state.continuationPlan;
    const contract = state.session.route.continuation_contract;
    const policy = state.session.route.continuation_policy;

    if (plan.resolution_status === "disabled" || !contract) {
      const disabled = ContinuationBinder.makeBinding({
        plan_hash: plan.plan_hash,
        effective_mode: "disabled",
        anchor_class: contract?.anchor_class ?? "none",
        selected_anchor_id: null,
        upstream_handle: null,
        covered_turn_count: 0,
        delta_start_turn_index: 0,
        delta_end_turn_index: plan.visible_turn_count,
        store_upstream_state: false,
        re_specify_head_fields: contract?.re_specify_head_on_delta ?? true,
        decision_reason: plan.decision_reason,
        rejected_candidates_audit: [],
        verified_anchor_material_bindings: [],
      });
      state.continuationBinding = disabled;
      return disabled;
    }

    const history = state.contextPlan.visible_history;
    const chain = plan.turn_prefix_chain;
    const anchorClass = contract.anchor_class;
    const storeUpstreamState = contract.requires_upstream_store && plan.reusable_on_commit;

    // First turn (genesis) or forced pre-seal reopen
    if (history.length === 0 || options?.reopen) {
      const rootBinding = ContinuationBinder.makeBinding({
        plan_hash: plan.plan_hash,
        effective_mode: storeUpstreamState ? "stateful_root_init" : "stateless_full_replay",
        anchor_class: anchorClass,
        selected_anchor_id: null,
        upstream_handle: null,
        covered_turn_count: 0,
        delta_start_turn_index: 0,
        delta_end_turn_index: history.length,
        store_upstream_state: storeUpstreamState,
        re_specify_head_fields: contract.re_specify_head_on_delta,
        decision_reason: options?.reopen ? "pre_seal_reopen_full_replay" : "genesis_turn",
        rejected_candidates_audit: [],
        verified_anchor_material_bindings: [],
      });
      state.continuationBinding = rootBinding;
      return rootBinding;
    }

    // Special guard for mutable_session_container: taint on any unreconciled indeterminate request
    if (
      anchorClass === "mutable_session_container" &&
      store.hasUnreconciledIndeterminateRequest(state.session.id)
    ) {
      const tainted = ContinuationBinder.makeBinding({
        plan_hash: plan.plan_hash,
        effective_mode: storeUpstreamState ? "stateful_root_init" : "stateless_full_replay",
        anchor_class: anchorClass,
        selected_anchor_id: null,
        upstream_handle: null,
        covered_turn_count: 0,
        delta_start_turn_index: 0,
        delta_end_turn_index: history.length,
        store_upstream_state: storeUpstreamState,
        re_specify_head_fields: contract.re_specify_head_on_delta,
        decision_reason: "mutable_container_tainted",
        rejected_candidates_audit: [],
        verified_anchor_material_bindings: [],
      });
      state.continuationBinding = tainted;
      return tainted;
    }

    const candidates = store.listSessionAnchors(state.session.id);
    const rejections: RejectedCandidateAudit[] = [];
    let selected: CommittedContinuationAnchor | null = null;
    let verifiedCoveredRefs: MaterialOccurrenceRef[] = [];

    const invalidateOnAffinity = policy?.invalidate_on_affinity_rotation ?? false;

    for (const candidate of candidates) {
      const aid = candidate.id;
      const covered = candidate.covered_turn_count;

      if (
        !ContinuationBinder.isAnchorScopeValid(
          candidate,
          state.session,
          plan.anchor_safety_margin_seconds
        )
      ) {
        rejections.push({ anchor_id: aid, reason: "scope_state_or_ttl_invalid" });
        continue;
      }

      if (candidate.parent_head_fingerprint !== plan.parent_head_fingerprint) {
        rejections.push({ anchor_id: aid, reason: "parent_head_fingerprint_mismatch" });
        continue;
      }

      if (invalidateOnAffinity && candidate.affinity_epoch !== state.wireAffinityEpoch) {
        rejections.push({ anchor_id: aid, reason: "affinity_epoch_rotated" });
        continue;
      }

      if (covered < 1 || covered > history.length) {
        rejections.push({ anchor_id: aid, reason: "covered_turn_count_out_of_range" });
        continue;
      }

      const deltaCount = history.length - covered;
      if (
        (anchorClass === "linear_ephemeral_cursor" ||
          anchorClass === "mutable_session_container") &&
        (covered !== history.length ||
          candidate.history_version !== state.session.history_version)
      ) {
        rejections.push({ anchor_id: aid, reason: "linear_continuity_broken" });
        continue;
      }

      if (!plan.allow_multiturn_delta && deltaCount > 0) {
        rejections.push({ anchor_id: aid, reason: "multiturn_delta_not_supported" });
        continue;
      }

      if (deltaCount > plan.max_delta_turns) {
        rejections.push({ anchor_id: aid, reason: "max_delta_turns_exceeded" });
        continue;
      }

      const expectedChainEntry = chain[covered - 1];
      if (!expectedChainEntry || candidate.turn_chain_digest !== expectedChainEntry.turn_chain_digest) {
        rejections.push({ anchor_id: aid, reason: "not_true_ordered_prefix" });
        continue;
      }

      // Check material refs & live binding_generation match
      const expectedCoveredRefs = state.historicalPinnedBindings.filter(
        (ref) => ref.history_entry_index < covered
      );
      if (stableHash(candidate.covered_material_bindings) !== stableHash(expectedCoveredRefs)) {
        rejections.push({ anchor_id: aid, reason: "ANCHOR_MATERIAL_GENERATION_MISMATCH" });
        continue;
      }

      let genMismatch = false;
      const nowMs = Date.now();
      const marginMs = plan.anchor_safety_margin_seconds * 1000;
      for (const ref of expectedCoveredRefs) {
        const live = state.liveBindings.get(ref.material_id);
        if (
          !live ||
          live.status !== "ready" ||
          live.binding_generation !== ref.binding_generation ||
          live.provider_binding_id !== ref.provider_binding_id ||
          live.external_uri !== ref.external_uri ||
          new Date(live.expires_at).getTime() <= nowMs + marginMs
        ) {
          genMismatch = true;
          break;
        }
      }
      if (genMismatch) {
        rejections.push({ anchor_id: aid, reason: "ANCHOR_MATERIAL_GENERATION_MISMATCH" });
        continue;
      }

      // Closed-loop parent chain + delta material coverage check (§1.3)
      const coverageCheck = verifyParentChainMaterialCoverage({
        placementPlan: state.contextPlan.placement_plan,
        anchorMaterialBindings: expectedCoveredRefs,
        deltaTurns: history.slice(covered),
      });
      if (!coverageCheck.ok) {
        rejections.push({
          anchor_id: aid,
          reason: coverageCheck.reason ?? "material_coverage_gap",
        });
        continue;
      }

      selected = candidate;
      verifiedCoveredRefs = expectedCoveredRefs;
      break;
    }

    if (selected) {
      const matched = ContinuationBinder.makeBinding({
        plan_hash: plan.plan_hash,
        effective_mode: "stateful_anchor_delta",
        anchor_class: anchorClass,
        selected_anchor_id: selected.id,
        upstream_handle: selected.upstream_handle,
        covered_turn_count: selected.covered_turn_count,
        delta_start_turn_index: selected.covered_turn_count,
        delta_end_turn_index: history.length,
        store_upstream_state: storeUpstreamState,
        re_specify_head_fields: contract.re_specify_head_on_delta,
        decision_reason:
          selected.covered_turn_count === history.length
            ? "matched_latest_committed_anchor"
            : "matched_longest_true_prefix",
        rejected_candidates_audit: rejections,
        verified_anchor_material_bindings: verifiedCoveredRefs,
      });
      state.continuationBinding = matched;
      return matched;
    }

    // Pre-dispatch fallback: lossless full replay / new root init
    const fallback = ContinuationBinder.makeBinding({
      plan_hash: plan.plan_hash,
      effective_mode: storeUpstreamState ? "stateful_root_init" : "stateless_full_replay",
      anchor_class: anchorClass,
      selected_anchor_id: null,
      upstream_handle: null,
      covered_turn_count: 0,
      delta_start_turn_index: 0,
      delta_end_turn_index: history.length,
      store_upstream_state: storeUpstreamState,
      re_specify_head_fields: contract.re_specify_head_on_delta,
      decision_reason:
        candidates.length > 0
          ? "no_eligible_ancestor_fallback_reopen"
          : "no_committed_anchors_root_init",
      rejected_candidates_audit: rejections,
      verified_anchor_material_bindings: [],
    });
    state.continuationBinding = fallback;
    return fallback;
  }
}

export class PhysicalContextProjector {
  static project(state: RuntimeExecutionState): void {
    const binding = state.continuationBinding;
    const deltaHistory = binding
      ? state.contextPlan.visible_history.slice(
          binding.delta_start_turn_index,
          binding.delta_end_turn_index
        )
      : state.contextPlan.visible_history;

    state.preparedPayloadDigest = stableHash({
      route_binding_hash: state.session.route.route_binding_hash,
      context_plan_hash: state.contextPlan.context_plan_hash,
      continuation_binding_hash: binding?.binding_hash ?? null,
      delta_history_count: deltaHistory.length,
      current_material_ids: state.currentMaterials.map((m) => m.id),
    });
  }

  /**
   * Unified re-projection entrypoint when pre-dispatch seal detects an invalidated/expired anchor.
   */
  static reprojectForContinuation(state: RuntimeExecutionState): ContinuationExecutionBinding {
    const reopened = ContinuationBinder.bindForContext(state, { reopen: true });
    PhysicalContextProjector.project(state);
    return reopened;
  }
}

export class AtomicDispatchSealer {
  static sealBeforeDispatch(
    state: RuntimeExecutionState
  ): { ok: true; bundle: SealedDispatchBundle } | { ok: false; code: string; message: string } {
    const binding = state.continuationBinding;
    const isSealedContinuation =
      state.continuationPlan.schema_version === "relay-continuation-plan/2" &&
      binding !== null &&
      binding.effective_mode !== "disabled";

    const materialDigest = stableHash(
      Array.from(state.liveBindings.values()).map((b) => ({
        material_id: b.material_id,
        binding_generation: b.binding_generation,
        provider_binding_id: b.provider_binding_id,
        external_uri: b.external_uri,
      }))
    );

    if (isSealedContinuation && binding) {
      const sealRes = store.sealContinuationContext({
        session: state.session,
        request: state.request,
        binding,
        preparedPayloadHash: state.preparedPayloadDigest,
        anchorSafetyMarginSeconds: state.continuationPlan.anchor_safety_margin_seconds,
      });
      if (!sealRes.ok) {
        return sealRes;
      }
    } else {
      if (state.request.provider_dispatch_state !== "not_sent") {
        return {
          ok: false,
          code: "DISPATCH_FENCE_ALREADY_CROSSED",
          message: `Cannot seal request in state ${state.request.provider_dispatch_state}`,
        };
      }
      state.request.prepared_payload_hash = state.preparedPayloadDigest;
      state.request.provider_dispatch_state = "dispatch_started";
    }

    const bundleBase = {
      seal_version: "relay-dispatch-seal/2" as const,
      request_id: state.request.id,
      session_id: state.session.id,
      history_version: state.session.history_version,
      route_binding_hash: state.session.route.route_binding_hash,
      context_plan_hash: state.contextPlan.context_plan_hash,
      continuation_binding_hash: binding?.binding_hash ?? null,
      material_binding_digest: materialDigest,
      prepared_payload_digest: state.preparedPayloadDigest,
    };
    const sealHash = stableHash(bundleBase);
    const bundle: SealedDispatchBundle = { ...bundleBase, seal_hash: sealHash };
    state.sealedBundle = bundle;
    state.request.prepared_payload_hash = sealHash;
    return { ok: true, bundle };
  }
}

/**
 * Protocol-neutral post-dispatch anchor rejection classifier.
 * Matches upstream 400/404/410 errors referencing the configured request_anchor_field or handle.
 */
export function isUpstreamAnchorRejected(params: {
  upstreamHttpStatus?: number;
  message: string;
  bodyText?: string;
  binding: ContinuationExecutionBinding | null;
  profileContinuation?: { request_anchor_field?: string };
}): boolean {
  if (!params.binding?.selected_anchor_id || !params.binding.upstream_handle) {
    return false;
  }
  const status = params.upstreamHttpStatus;
  if (status !== 400 && status !== 404 && status !== 410) {
    return false;
  }
  const combined = `${params.message}\n${params.bodyText || ""}`.toLowerCase();
  const fieldName = (
    params.profileContinuation?.request_anchor_field || "previous_interaction_id"
  ).toLowerCase();
  const handleLower = params.binding.upstream_handle.toLowerCase();

  if (combined.includes(handleLower) || combined.includes(fieldName)) {
    return true;
  }
  if (
    status === 404 &&
    (combined.includes("interaction") ||
      combined.includes("not found") ||
      combined.includes("previous_response"))
  ) {
    return true;
  }
  return false;
}
