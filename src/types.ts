import { z } from "zod";
import type {
  ContinuationAnchorClass,
  ContinuationCapabilityContract,
  ContinuationMode,
  ContinuationPolicySpec,
} from "./continuationContracts.js";

export const ContextPolicySchema = z.enum(["conversation", "explicit"]);
export const RequestContextPolicySchema = z.enum([
  "conversation",
  "explicit",
  "session_materials_only",
]);
export const ExecutionModeSchema = z.enum(["sync", "async"]);
export const MaterialPurposeSchema = z.enum(["inference_input", "archive"]);
export const CacheModeSchema = z.enum(["off", "auto", "on"]);
export const CacheMechanismPreferenceSchema = z.enum([
  "auto",
  "implicit_prefix",
  "stateful_resource",
]);

export const SessionCapabilityRequirementsSchema = z.object({
  input_modalities: z.array(z.string()).default(["text"]),
  structured_output: z.enum(["none", "post_validate", "native"]).default("none"),
  think_levels: z.array(z.string()).default([]),
  required_features: z.array(z.string()).default([]),
  channel_allowlist: z.array(z.string()).default([]),
});

export const SessionContextPolicyObjectSchema = z.object({
  history_mode: z.enum(["cumulative", "ephemeral_turn"]).default("cumulative"),
  sticky_scope: z.enum(["session", "conversation_family"]).default("session"),
  workload_hint: z.enum(["auto", "multimodal_or_long_doc", "pure_text_qa"]).default("auto"),
});

export const SessionCreateRequestSchema = z.object({
  tenant_id: z.string().min(1).max(200),
  conversation_hash: z.string().min(1).max(256),
  provider: z.string().min(1).max(80),
  model: z.string().min(1).max(200),
  connection_id: z.string().max(120).nullable().optional(),
  context_policy: z
    .union([ContextPolicySchema, SessionContextPolicyObjectSchema])
    .default("conversation"),
  material_ids: z.array(z.string()).default([]),
  execution_pool: z.string().max(120).nullable().optional(),
  capability_requirements: SessionCapabilityRequirementsSchema.default(() => ({
    input_modalities: ["text"],
    structured_output: "none" as const,
    think_levels: [],
    required_features: [],
    channel_allowlist: [],
  })),
  metadata: z.record(z.string(), z.unknown()).default(() => ({})),
});

export const SessionRequestCreateSchema = z.object({
  input: z.unknown(),
  context_policy: RequestContextPolicySchema.nullable().optional(),
  instructions: z.string().nullable().optional(),
  tools: z.array(z.unknown()).optional(),
  material_ids: z.array(z.string()).default([]),
  provider: z.string().max(80).nullable().optional(),
  connection_id: z.string().max(120).nullable().optional(),
  model: z.string().max(200).nullable().optional(),
  think_level: z.string().max(40).nullable().optional(),
  think_level_policy: z.enum(["prefer", "require"]).default("prefer"),
  execution: z.object({
    mode: ExecutionModeSchema,
  }),
  structured_output: z.record(z.string(), z.unknown()).default(() => ({})),
  options: z.record(z.string(), z.unknown()).default(() => ({})),
  provider_payload: z.record(z.string(), z.unknown()).default(() => ({})),
  requested_cache_mode: CacheModeSchema.default("auto"),
  requested_cache_mechanism: CacheMechanismPreferenceSchema.default("auto"),
  continuation_mode: z.string().optional(),
  metadata: z.record(z.string(), z.unknown()).default(() => ({})),
});

export const MaterialCreateJSONSchema = z
  .object({
    tenant_id: z.string().min(1).max(200),
    conversation_hash: z.string().min(1).max(256),
    filename: z.string().min(1).max(500),
    content_type: z.string().max(250).nullable().optional(),
    source_url: z.string().nullable().optional(),
    content_base64: z.string().nullable().optional(),
    source_ref: z.string().max(1000).nullable().optional(),
    parent_material_id: z.string().max(220).nullable().optional(),
    ordinal: z.number().int().nullable().optional(),
    provider: z.string().max(80).nullable().optional(),
    model: z.string().max(200).nullable().optional(),
    purpose: MaterialPurposeSchema.default("inference_input"),
    target_connection_id: z.string().max(120).nullable().optional(),
    durability_policy: z.enum(["native_first", "relay_backed"]).default("native_first"),
    fallback_policy: z
      .enum(["never", "on_provider_unavailable", "always"])
      .default("on_provider_unavailable"),
    declared_size: z.number().int().nonnegative().nullable().optional(),
    request_file_total_bytes: z.number().int().nonnegative().nullable().optional(),
    request_file_count: z.number().int().positive().nullable().optional(),
    material_batch_id: z.string().max(220).nullable().optional(),
    metadata: z.record(z.string(), z.unknown()).default(() => ({})),
  })
  .superRefine((val, ctx) => {
    const count = Number(Boolean(val.source_url)) + Number(Boolean(val.content_base64));
    if (count !== 1) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "exactly one of source_url or content_base64 is required",
      });
    }
    if (val.purpose === "inference_input" && (!val.provider || !val.model)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "provider and model are required for inference_input material",
      });
    }
  });

export const FUSION_STAGES = new Set([
  "fusion_corpus_ingest",
  "material_evidence_mapping",
  "global_adjudication",
  "scoped_decision",
  "final_evidence_review",
  "direct_final_synthesis",
  "synthesis_blueprint",
  "final_draft_generation",
  "quality_review",
  "evidence_grounded_repair",
]);

export const JobSubmitRequestSchema = z.object({
  tenant_id: z.string().min(1).max(200),
  conversation_hash: z.string().min(1).max(256),
  relay_session_id: z.string().uuid().nullable().optional(),
  stage: z.string().min(1).max(120),
  provider: z.string().min(1).max(80),
  model: z.string().min(1).max(200),
  think_level: z.enum(["auto", "low", "medium", "high", "xhigh"]).default("auto"),
  mode: z
    .enum(["new_session", "continue_session", "stateless", "new_fusion_corpus"])
    .default("stateless"),
  current_query: z.string().nullable().optional(),
  instructions: z.string().nullable().optional(),
  material_prefix: z.unknown().optional(),
  material_prefix_includes_current_query: z.boolean().default(false),
  material_expires_at: z.string().nullable().optional(),
  fusion_corpus_id: z.string().max(220).nullable().optional(),
  route_profile: z.record(z.string(), z.unknown()).default(() => ({})),
  payload: z.record(z.string(), z.unknown()).default(() => ({})),
  upstream: z.object({ base_url: z.string().nullable().optional() }).nullable().optional(),
  provider_payload: z.record(z.string(), z.unknown()).default(() => ({})),
  structured_output: z.record(z.string(), z.unknown()).default(() => ({})),
  metadata: z.record(z.string(), z.unknown()).default(() => ({})),
});

export interface RouteBinding {
  provider: string;
  model: string;
  connection_id: string;
  channel_id: string;
  protocol: string;
  offering_id: string;
  capability_contract_id: string;
  protocol_profile_id: string;
  cache_policy_id: string;
  account_scope_id: string;
  account_scope_hash: string;
  connection_spec_hash: string;
  protocol_profile_hash: string;
  zero_data_retention: boolean;
  route_revision: string;
  capability_revision: string;
  control_plane_hash: string;
  cache_contract_hash: string;
  execution_pool: string;
  route_binding_hash: string;
  upstream_model: string;
  protocol_profile_continuation?: Record<string, unknown>;
  continuation_contract_hash?: string;
  continuation_contract?: ContinuationCapabilityContract;
  continuation_policy_id?: string;
  continuation_policy_hash?: string;
  continuation_policy?: ContinuationPolicySpec;
}

export interface MaterialRecord {
  id: string;
  tenant_id: string;
  conversation_hash: string;
  idempotency_key: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  provider: string | null;
  model: string | null;
  purpose: "inference_input" | "archive";
  status: string;
  durability: string;
  target_connection_id: string | null;
  object_id: string;
  storage_id: string;
  source_ref: string | null;
  parent_material_id: string | null;
  ordinal: number | null;
  route_revision: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface ProviderMaterialBindingRecord {
  material_id: string;
  account_scope_id: string;
  connection_id: string;
  binding_generation: number;
  provider_binding_id: string | null;
  external_uri: string | null;
  representation: string;
  status: "ready" | "expired" | "invalidated";
  expires_at: string;
}

export interface MaterialOccurrenceRef {
  material_id: string;
  history_entry_index: number;
  binding_generation: number;
  provider_binding_id: string | null;
  external_uri: string | null;
  representation: string;
  part_digest: string;
}

export interface SessionRecord {
  id: string;
  tenant_id: string;
  conversation_hash: string;
  provider: string;
  model: string;
  connection_id: string;
  context_policy: "conversation" | "explicit";
  history_mode: "cumulative" | "ephemeral_turn";
  sticky_scope: "session" | "conversation_family";
  material_ids: string[];
  history_version: number;
  route_pin_state: string;
  supply_group_id: string | null;
  active_request_id: string | null;
  execution_pool: string;
  idempotency_key: string;
  session_hash: string;
  route: RouteBinding;
  committed_material_ledger: MaterialOccurrenceRef[];
  metadata: Record<string, unknown>;
  created_at: string;
  expires_at: string;
}

export interface NativeStepBlock {
  type: string;
  text?: string;
  signature?: string;
  summary?: Array<{ type: string; text?: string }>;
  content?: Array<Record<string, unknown>>;
  [key: string]: unknown;
}

export interface HistoryTurn {
  role: "user" | "model";
  text: string;
  material_ids?: string[];
  material_occurrences?: MaterialOccurrenceRef[];
  native_steps?: NativeStepBlock[];
  request_id: string;
  timestamp: string;
}

export type ProviderDispatchState =
  | "not_sent"
  | "dispatch_started"
  | "result_stored"
  | "committed"
  | "failed"
  | "indeterminate";

export interface TurnPrefixChainEntry {
  turn_index: number;
  turn_digest: string;
  turn_chain_digest: string;
}

export interface EffectiveMaterialPlacementPlan {
  version: "material-placement/2";
  session_manifest_auto: string[];
  request_explicit: string[];
  history_committed: string[];
  historical_only_material_ids: string[];
  current_effective_material_ids: string[];
  plan_digest: string;
}

export interface ContinuationTopology {
  head_format: string;
  parent_head_fingerprint: string;
  visible_turn_count: number;
  turn_prefix_chain: TurnPrefixChainEntry[];
  reusable_on_commit: boolean;
}

export interface ContextPlan {
  schema_version: "relay-context-plan/2";
  model_history_visible: boolean;
  effective_policy: "conversation" | "explicit" | "session_materials_only";
  instructions: string | null;
  tools: unknown[];
  visible_history: HistoryTurn[];
  placement_plan: EffectiveMaterialPlacementPlan;
  continuation_topology: ContinuationTopology;
  context_plan_hash: string;
}

export interface ContinuationIntentPlan {
  schema_version: "relay-continuation-plan/2";
  requested_mode: ContinuationMode | string;
  planned_anchor_class: ContinuationAnchorClass;
  resolution_status: "disabled" | "pending_runtime_bind";
  decision_reason: string;
  context_plan_hash: string;
  capability_contract_hash: string;
  protocol_profile_hash: string;
  continuation_policy_hash: string | null;
  model_history_visible: boolean;
  reusable_on_commit: boolean;
  head_format: string;
  parent_head_fingerprint: string;
  visible_turn_count: number;
  turn_prefix_chain: TurnPrefixChainEntry[];
  required_visible_material_ids: string[];
  allow_multiturn_delta: boolean;
  allow_stateless_fallback: boolean;
  allow_head_mismatch_reopen: boolean;
  max_delta_turns: number;
  anchor_safety_margin_seconds: number;
  plan_hash: string;
}

export interface RejectedCandidateAudit {
  anchor_id: string;
  reason: string;
}

export interface ContinuationExecutionBinding {
  binding_version: "relay-continuation-binding/2";
  plan_hash: string;
  effective_mode:
    | "disabled"
    | "stateless_full_replay"
    | "stateful_root_init"
    | "stateful_anchor_delta";
  anchor_class: ContinuationAnchorClass;
  selected_anchor_id: string | null;
  upstream_handle: string | null;
  covered_turn_count: number;
  delta_start_turn_index: number;
  delta_end_turn_index: number;
  store_upstream_state: boolean;
  re_specify_head_fields: boolean;
  decision_reason: string;
  rejected_candidates_audit: RejectedCandidateAudit[];
  verified_anchor_material_bindings: MaterialOccurrenceRef[];
  binding_hash: string;
}

export interface CommittedContinuationAnchor {
  id: string;
  session_id: string;
  tenant_id: string;
  conversation_hash: string;
  source_request_id: string;
  parent_anchor_id: string | null;
  connection_id: string;
  account_scope_id: string;
  account_scope_hash: string;
  connection_spec_hash: string;
  protocol: string;
  model: string;
  anchor_class: ContinuationAnchorClass;
  upstream_handle: string;
  affinity_epoch: number | null;
  history_version: number;
  is_root_prefix: boolean;
  covered_turn_count: number;
  parent_head_fingerprint: string;
  turn_chain_digest: string;
  covered_material_bindings: MaterialOccurrenceRef[];
  covered_materials_digest: string;
  status: "active" | "superseded" | "expired" | "invalidated";
  expires_at: string;
  invalidated_by_request_id: string | null;
  invalidation_reason: string | null;
  created_at: string;
  updated_at: string;
}

export interface AnchorPinRecord {
  request_id: string;
  anchor_id: string;
  status: "active" | "held" | "released";
  created_at: string;
  released_at: string | null;
}

export interface SealedDispatchBundle {
  seal_version: "relay-dispatch-seal/2";
  request_id: string;
  session_id: string;
  history_version: number;
  route_binding_hash: string;
  context_plan_hash: string;
  continuation_binding_hash: string | null;
  material_binding_digest: string;
  prepared_payload_digest: string;
  seal_hash: string;
}

export interface ActualExecutionFact {
  schema_version: "relay-execution-fact/2";
  request_id: string;
  session_id: string;
  seal_hash: string;
  continuation_execution_binding: ContinuationExecutionBinding;
  upstream_anchor_ref: string | null;
  upstream_state_stored: boolean;
  previous_anchor_ref: string | null;
  native_steps: NativeStepBlock[];
  text: string;
  raw_text: string;
  model: string;
  usage: {
    prompt_tokens: number | null;
    completion_tokens: number | null;
    total_tokens: number | null;
    cached_tokens: number | null;
    thinking_tokens: number | null;
    omitted_history_estimated_tokens: number;
  };
  affinity_epoch: number | null;
  created_at: string;
}

export interface RequestRecord {
  id: string;
  session_id: string;
  tenant_id: string;
  conversation_hash: string;
  job_id: string | null;
  idempotency_key: string;
  caller_intent_hash: string;
  execution_mode: "sync" | "async";
  status: "queued" | "leased" | "running" | "succeeded" | "failed" | "cancelled" | "indeterminate";
  provider_dispatch_state: ProviderDispatchState;
  fence_owner: string;
  fence_epoch: number;
  history_version: number;
  expected_history_version: number;
  continuation_plan_object_id: string | null;
  continuation_plan_hash: string | null;
  sealed_continuation_binding: ContinuationExecutionBinding | null;
  prepared_payload_hash: string | null;
  produced_anchor_id: string | null;
  result: Record<string, unknown> | null;
  error: Record<string, unknown> | null;
  cache: Record<string, unknown> | null;
  continuation: Record<string, unknown> | null;
  raw_error_bytes: Buffer | null;
  raw_error_content_type: string | null;
  created_at: string;
  completed_at: string | null;
}

export interface JobRecord {
  id: string;
  tenant_id: string;
  conversation_hash: string;
  relay_session_id: string | null;
  stage: string;
  provider: string;
  model: string;
  think_level: string;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled" | "expired";
  idempotency_key: string;
  compact_result: Record<string, unknown> | null;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  expires_at: string;
}
