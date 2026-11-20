export type ContinuationAnchorClass =
  | "none"
  | "immutable_tree_anchor"
  | "linear_ephemeral_cursor"
  | "mutable_session_container";

export type ContinuationVerificationStatus = "unverified" | "verified" | "disabled";

export type ContinuationMode =
  | "off"
  | "prefer_stateful_anchor"
  | "require_stateful_anchor";

export const VALID_ANCHOR_CLASSES = new Set<ContinuationAnchorClass>([
  "none",
  "immutable_tree_anchor",
  "linear_ephemeral_cursor",
  "mutable_session_container",
]);

/**
 * Strict certified allowlist of protocols with full runtime continuation & anchor support.
 * Uncertified protocols deterministically resolve to disabled rather than writing dirty anchors.
 */
export const IMPLEMENTED_CONTINUATION_PROTOCOLS = new Set<string>([
  "gemini_interactions",
]);

export interface ContinuationCapabilityContract {
  version: string;
  anchor_class: ContinuationAnchorClass;
  verification_status: ContinuationVerificationStatus;
  supports_branching: boolean;
  supports_multiturn_delta: boolean;
  requires_upstream_store: boolean;
  forbid_when_zero_retention: boolean;
  mutually_exclusive_cache_mechanisms: string[];
  retention_ttl_seconds: number;
  anchor_safety_margin_seconds: number;
  head_invariants: string[];
  re_specify_head_on_delta: boolean;
  requires_thought_signature_on_replay: boolean;
  max_delta_turns: number;
}

export interface ContinuationPolicySpec {
  policy_id: string;
  revision: string;
  default_mode: ContinuationMode;
  allow_request_override: boolean;
  allow_stateless_fallback_pre_dispatch: boolean;
  allow_head_mismatch_reopen: boolean;
  invalidate_on_affinity_rotation: boolean;
  max_active_anchors_per_session: number;
  metadata?: Record<string, unknown>;
}

export function buildContinuationCapabilityContract(
  overrides: Partial<ContinuationCapabilityContract> = {}
): ContinuationCapabilityContract {
  return {
    version: overrides.version ?? "relay-continuation-contract/1",
    anchor_class: overrides.anchor_class ?? "none",
    verification_status: overrides.verification_status ?? "disabled",
    supports_branching: overrides.supports_branching ?? false,
    supports_multiturn_delta: overrides.supports_multiturn_delta ?? false,
    requires_upstream_store: overrides.requires_upstream_store ?? true,
    forbid_when_zero_retention: overrides.forbid_when_zero_retention ?? true,
    mutually_exclusive_cache_mechanisms: overrides.mutually_exclusive_cache_mechanisms
      ? [...overrides.mutually_exclusive_cache_mechanisms]
      : [],
    retention_ttl_seconds: overrides.retention_ttl_seconds ?? 86400,
    anchor_safety_margin_seconds: overrides.anchor_safety_margin_seconds ?? 300,
    head_invariants: overrides.head_invariants
      ? [...overrides.head_invariants]
      : ["instructions", "tools", "account_scope_id", "model"],
    re_specify_head_on_delta: overrides.re_specify_head_on_delta ?? true,
    requires_thought_signature_on_replay:
      overrides.requires_thought_signature_on_replay ?? true,
    max_delta_turns: overrides.max_delta_turns ?? 64,
  };
}

export function canonicalContinuationContract(
  contract: ContinuationCapabilityContract | undefined
): Record<string, unknown> | undefined {
  if (!contract || contract.anchor_class === "none") {
    return undefined;
  }
  return {
    version: contract.version,
    anchor_class: contract.anchor_class,
    verification_status: contract.verification_status,
    supports_branching: contract.supports_branching,
    supports_multiturn_delta: contract.supports_multiturn_delta,
    requires_upstream_store: contract.requires_upstream_store,
    forbid_when_zero_retention: contract.forbid_when_zero_retention,
    mutually_exclusive_cache_mechanisms: [...contract.mutually_exclusive_cache_mechanisms],
    retention_ttl_seconds: contract.retention_ttl_seconds,
    anchor_safety_margin_seconds: contract.anchor_safety_margin_seconds,
    head_invariants: [...contract.head_invariants],
    re_specify_head_on_delta: contract.re_specify_head_on_delta,
    requires_thought_signature_on_replay: contract.requires_thought_signature_on_replay,
    max_delta_turns: contract.max_delta_turns,
  };
}

export function canonicalContinuationPolicy(
  policy: ContinuationPolicySpec | undefined
): Record<string, unknown> | undefined {
  if (!policy) return undefined;
  return {
    policy_id: policy.policy_id,
    revision: policy.revision,
    default_mode: policy.default_mode,
    allow_request_override: policy.allow_request_override,
    allow_stateless_fallback_pre_dispatch: policy.allow_stateless_fallback_pre_dispatch,
    allow_head_mismatch_reopen: policy.allow_head_mismatch_reopen,
    invalidate_on_affinity_rotation: policy.invalidate_on_affinity_rotation,
    max_active_anchors_per_session: policy.max_active_anchors_per_session,
    ...(policy.metadata && Object.keys(policy.metadata).length > 0
      ? { metadata: policy.metadata }
      : {}),
  };
}

export class ContinuationDecisionError extends Error {
  readonly code: string;
  readonly httpStatus: number;

  constructor(code: string, message: string, httpStatus = 422) {
    super(message);
    this.name = "ContinuationDecisionError";
    this.code = code;
    this.httpStatus = httpStatus;
  }
}

export interface CrossContractValidationInput {
  connection: {
    protocol: string;
    zero_data_retention?: boolean;
    metadata?: Record<string, unknown>;
  };
  capability: {
    continuation?: ContinuationCapabilityContract;
    cache?: { supported_mechanisms?: string[] };
    metadata?: Record<string, unknown>;
  };
  profile: {
    protocol: string;
    continuation?: {
      anchor_class?: ContinuationAnchorClass;
      request_anchor_field?: string;
      response_anchor_field?: string;
      wire_endpoint?: string;
    };
    cache?: { supported_mechanisms?: string[] };
  } | null;
  policy: ContinuationPolicySpec | null;
}

export function validateContinuationCrossContract(
  input: CrossContractValidationInput
): void {
  const cont = input.capability.continuation;
  const anchorClass: ContinuationAnchorClass = cont?.anchor_class ?? "none";

  if (!VALID_ANCHOR_CLASSES.has(anchorClass)) {
    throw new Error(`Unknown continuation anchor_class: ${String(anchorClass)}`);
  }

  if (
    (anchorClass === "linear_ephemeral_cursor" ||
      anchorClass === "mutable_session_container") &&
    cont?.supports_branching === true
  ) {
    throw new Error(`${anchorClass} cannot declare supports_branching=true`);
  }

  const defaultMode: ContinuationMode = input.policy?.default_mode ?? "off";
  if (defaultMode !== "off" || anchorClass !== "none") {
    if (!IMPLEMENTED_CONTINUATION_PROTOCOLS.has(input.connection.protocol)) {
      throw new Error(
        `Protocol ${input.connection.protocol} has no certified continuation implementation`
      );
    }
    if (!input.profile?.continuation?.request_anchor_field) {
      throw new Error(
        `Protocol profile for ${input.connection.protocol} lacks continuation wire mapping`
      );
    }
    if (
      input.profile.continuation.anchor_class &&
      input.profile.continuation.anchor_class !== anchorClass
    ) {
      throw new Error(
        `Capability (${anchorClass}) and protocol profile (${input.profile.continuation.anchor_class}) disagree on continuation anchor_class`
      );
    }
  }

  const exclusive = new Set(cont?.mutually_exclusive_cache_mechanisms ?? []);
  if (input.connection.protocol === "gemini_interactions") {
    exclusive.add("stateful_resource");
  }
  const capCache = new Set(input.capability.cache?.supported_mechanisms ?? []);
  const profCache = new Set(input.profile?.cache?.supported_mechanisms ?? []);
  const conflicts = Array.from(exclusive).filter(
    (mech) => capCache.has(mech) || profCache.has(mech)
  );
  if (conflicts.length > 0) {
    throw new Error(
      `Continuation mutually exclusive cache mechanisms enabled: ${conflicts.sort().join(", ")}`
    );
  }

  const zdr = Boolean(
    input.connection.zero_data_retention ||
      input.connection.metadata?.zero_data_retention ||
      input.capability.metadata?.zero_data_retention
  );
  if (
    zdr &&
    defaultMode === "require_stateful_anchor" &&
    (cont?.requires_upstream_store ?? true)
  ) {
    throw new Error(
      "CONTINUATION_FORBIDDEN_BY_ZDR_POLICY: ZDR supply cannot require upstream stateful anchor"
    );
  }
}
