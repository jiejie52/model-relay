import { CAPABILITY_PROFILE_REVISION, config } from "./config.js";
import {
  buildContinuationCapabilityContract,
  canonicalContinuationContract,
  canonicalContinuationPolicy,
  type ContinuationAnchorClass,
  type ContinuationCapabilityContract,
  type ContinuationPolicySpec,
  validateContinuationCrossContract,
} from "./continuationContracts.js";
import { stableHash } from "./store.js";
import type { RouteBinding } from "./types.js";

export interface ConnectionSpec {
  connection_id: string;
  channel_id: string;
  protocol: string;
  account_scope_id: string;
  base_url: string;
  auth_type: string;
  zero_data_retention: boolean;
  metadata?: Record<string, unknown>;
}

export interface CapabilityContractSpec {
  contract_id: string;
  revision: string;
  input_modalities: string[];
  structured_output_mode: "none" | "post_validate" | "native";
  think_levels: string[];
  features: string[];
  cache: {
    supported_mechanisms: string[];
  };
  continuation?: ContinuationCapabilityContract;
  metadata?: Record<string, unknown>;
}

export interface ProtocolProfileSpec {
  profile_id: string;
  revision: string;
  protocol: string;
  supports_native_history_material_reuse: boolean;
  cache: {
    supported_mechanisms: string[];
  };
  continuation?: {
    anchor_class: ContinuationAnchorClass;
    request_anchor_field: string;
    response_anchor_field: string;
    wire_endpoint: string;
  };
  affinity_policy: {
    enabled: boolean;
  };
}

export interface OfferingSpec {
  offering_id: string;
  provider: string;
  model_pattern: string;
  connection_id: string;
  channel_id: string;
  protocol: string;
  capability_contract_id: string;
  protocol_profile_id: string;
  cache_policy_id: string;
  continuation_policy_id: string | null;
  priority: number;
  supports_continuation: boolean;
  supports_thinking: boolean;
}

const CONNECTIONS: Record<string, ConnectionSpec> = {
  gemini_google_interactions_prod: {
    connection_id: "gemini_google_interactions_prod",
    channel_id: "google_official",
    protocol: "gemini_interactions",
    account_scope_id: "google_official:default",
    base_url: config.aihubmixGeminiBaseUrl,
    auth_type: "x_goog_api_key_header",
    zero_data_retention: false,
  },
  gemini_google_official_prod: {
    connection_id: "gemini_google_official_prod",
    channel_id: "google_official",
    protocol: "gemini_native",
    account_scope_id: "google_official:default",
    base_url: config.aihubmixGeminiBaseUrl,
    auth_type: "x_goog_api_key_header",
    zero_data_retention: false,
  },
  aihubmix_chat_completions: {
    connection_id: "aihubmix_chat_completions",
    channel_id: "aihubmix",
    protocol: "chat_completions",
    account_scope_id: "aihubmix:default",
    base_url: config.aihubmixOpenaiBaseUrl,
    auth_type: "bearer",
    zero_data_retention: false,
  },
  aihubmix_default: {
    connection_id: "aihubmix_default",
    channel_id: "aihubmix",
    protocol: "responses",
    account_scope_id: "aihubmix:default",
    base_url: config.aihubmixOpenaiBaseUrl,
    auth_type: "bearer",
    zero_data_retention: false,
  },
  aihubmix_claude_messages: {
    connection_id: "aihubmix_claude_messages",
    channel_id: "aihubmix",
    protocol: "claude_messages",
    account_scope_id: "aihubmix:default",
    base_url: config.aihubmixClaudeBaseUrl,
    auth_type: "x_api_key",
    zero_data_retention: false,
  },
  moonshot_official: {
    connection_id: "moonshot_official",
    channel_id: "moonshot_official",
    protocol: "moonshot_chat",
    account_scope_id: "moonshot_official:default",
    base_url: config.moonshotBaseUrl,
    auth_type: "bearer",
    zero_data_retention: false,
  },
};

const CAPABILITY_CONTRACTS: Record<string, CapabilityContractSpec> = {
  "gemini-2.5-pro-google-interactions-capability": {
    contract_id: "gemini-2.5-pro-google-interactions-capability",
    revision: "10.3.0",
    input_modalities: ["text", "image", "audio", "video", "document"],
    structured_output_mode: "native",
    think_levels: ["auto", "minimal", "low", "medium", "high", "xhigh"],
    features: [],
    cache: {
      supported_mechanisms: ["implicit_prefix"],
    },
    continuation: buildContinuationCapabilityContract({
      anchor_class: "immutable_tree_anchor",
      verification_status: "verified",
      supports_branching: true,
      supports_multiturn_delta: true,
      requires_upstream_store: true,
      forbid_when_zero_retention: true,
      mutually_exclusive_cache_mechanisms: ["stateful_resource"],
      retention_ttl_seconds: 86400,
      anchor_safety_margin_seconds: 300,
      head_invariants: ["instructions", "tools", "account_scope_id", "model"],
      re_specify_head_on_delta: true,
      requires_thought_signature_on_replay: true,
      max_delta_turns: 64,
    }),
  },
  "gemini-2.5-pro-google-official-capability": {
    contract_id: "gemini-2.5-pro-google-official-capability",
    revision: "8.0.0-draft",
    input_modalities: ["text", "image", "document"],
    structured_output_mode: "native",
    think_levels: ["auto", "low", "medium", "high"],
    features: [],
    cache: {
      supported_mechanisms: ["stateful_resource", "implicit_prefix"],
    },
  },
  "gpt-6-reasoning": {
    contract_id: "gpt-6-reasoning",
    revision: "8.0.0-draft",
    input_modalities: ["text", "image"],
    structured_output_mode: "native",
    think_levels: ["auto", "low", "medium", "high"],
    features: [],
    cache: {
      supported_mechanisms: ["implicit_prefix"],
    },
  },
  "grok-4-7-responses": {
    contract_id: "grok-4-7-responses",
    revision: "8.0.0-draft",
    input_modalities: ["text", "image"],
    structured_output_mode: "native",
    think_levels: ["auto", "low", "medium", "high"],
    features: [],
    cache: {
      supported_mechanisms: ["implicit_prefix"],
    },
  },
  "claude-sonnet-5-adaptive": {
    contract_id: "claude-sonnet-5-adaptive",
    revision: "8.0.0-draft",
    input_modalities: ["text", "image", "document"],
    structured_output_mode: "post_validate",
    think_levels: ["auto", "low", "medium", "high"],
    features: [],
    cache: {
      supported_mechanisms: ["implicit_prefix"],
    },
  },
  "kimi-legacy-moonshot": {
    contract_id: "kimi-legacy-moonshot",
    revision: "8.0.0-draft",
    input_modalities: ["text"],
    structured_output_mode: "post_validate",
    think_levels: ["auto"],
    features: [],
    cache: {
      supported_mechanisms: ["implicit_prefix"],
    },
  },
};

const PROTOCOL_PROFILES: Record<string, ProtocolProfileSpec> = {
  "gemini-interactions-official-v1": {
    profile_id: "gemini-interactions-official-v1",
    revision: "10.3.0",
    protocol: "gemini_interactions",
    supports_native_history_material_reuse: true,
    cache: {
      supported_mechanisms: ["implicit_prefix"],
    },
    continuation: {
      anchor_class: "immutable_tree_anchor",
      request_anchor_field: "previous_interaction_id",
      response_anchor_field: "id",
      wire_endpoint: "/v1beta/interactions",
    },
    affinity_policy: { enabled: false },
  },
  "gemini-native-google-official": {
    profile_id: "gemini-native-google-official",
    revision: "8.0.0-draft",
    protocol: "gemini_native",
    supports_native_history_material_reuse: true,
    cache: {
      supported_mechanisms: ["stateful_resource", "implicit_prefix"],
    },
    affinity_policy: { enabled: false },
  },
  "chat-completions-aihubmix-v1": {
    profile_id: "chat-completions-aihubmix-v1",
    revision: "8.0.0-draft",
    protocol: "chat_completions",
    supports_native_history_material_reuse: false,
    cache: {
      supported_mechanisms: ["implicit_prefix"],
    },
    affinity_policy: { enabled: false },
  },
  "responses-aihubmix-v1": {
    profile_id: "responses-aihubmix-v1",
    revision: "8.0.0-draft",
    protocol: "responses",
    supports_native_history_material_reuse: false,
    cache: {
      supported_mechanisms: ["implicit_prefix"],
    },
    affinity_policy: { enabled: false },
  },
  "claude-messages-aihubmix-v1": {
    profile_id: "claude-messages-aihubmix-v1",
    revision: "8.0.0-draft",
    protocol: "claude_messages",
    supports_native_history_material_reuse: false,
    cache: {
      supported_mechanisms: ["implicit_prefix"],
    },
    affinity_policy: { enabled: false },
  },
  "moonshot-chat-v1": {
    profile_id: "moonshot-chat-v1",
    revision: "8.0.0-draft",
    protocol: "moonshot_chat",
    supports_native_history_material_reuse: false,
    cache: {
      supported_mechanisms: ["implicit_prefix"],
    },
    affinity_policy: { enabled: false },
  },
};

const CONTINUATION_POLICIES: Record<string, ContinuationPolicySpec> = {
  "default-prefer-stateful-anchor": {
    policy_id: "default-prefer-stateful-anchor",
    revision: "10.3.0",
    default_mode: "prefer_stateful_anchor",
    allow_request_override: true,
    allow_stateless_fallback_pre_dispatch: true,
    allow_head_mismatch_reopen: true,
    invalidate_on_affinity_rotation: false,
    max_active_anchors_per_session: 128,
  },
};

const BUILTIN_OFFERINGS: OfferingSpec[] = [
  {
    offering_id: "gemini-google-interactions",
    provider: "gemini",
    model_pattern: "gemini-*",
    connection_id: "gemini_google_interactions_prod",
    channel_id: "google_official",
    protocol: "gemini_interactions",
    capability_contract_id: "gemini-2.5-pro-google-interactions-capability",
    protocol_profile_id: "gemini-interactions-official-v1",
    cache_policy_id: "google-official-session-cache",
    continuation_policy_id: "default-prefer-stateful-anchor",
    priority: 100,
    supports_continuation: true,
    supports_thinking: true,
  },
  {
    offering_id: "gemini-google-native",
    provider: "gemini",
    model_pattern: "gemini-*",
    connection_id: "gemini_google_official_prod",
    channel_id: "google_official",
    protocol: "gemini_native",
    capability_contract_id: "gemini-2.5-pro-google-official-capability",
    protocol_profile_id: "gemini-native-google-official",
    cache_policy_id: "google-official-session-cache",
    continuation_policy_id: null,
    priority: 90,
    supports_continuation: false,
    supports_thinking: true,
  },
  {
    offering_id: "openai-responses-aihubmix",
    provider: "openai",
    model_pattern: "*",
    connection_id: "aihubmix_chat_completions",
    channel_id: "aihubmix",
    protocol: "chat_completions",
    capability_contract_id: "gpt-6-reasoning",
    protocol_profile_id: "chat-completions-aihubmix-v1",
    cache_policy_id: "session-private-cache-policy-v1",
    continuation_policy_id: null,
    priority: 80,
    supports_continuation: false,
    supports_thinking: true,
  },
  {
    offering_id: "grok-responses-aihubmix",
    provider: "grok",
    model_pattern: "grok-*",
    connection_id: "aihubmix_default",
    channel_id: "aihubmix",
    protocol: "responses",
    capability_contract_id: "grok-4-7-responses",
    protocol_profile_id: "responses-aihubmix-v1",
    cache_policy_id: "session-private-cache-policy-v1",
    continuation_policy_id: null,
    priority: 80,
    supports_continuation: false,
    supports_thinking: true,
  },
  {
    offering_id: "claude-messages-aihubmix",
    provider: "claude",
    model_pattern: "claude-*",
    connection_id: "aihubmix_claude_messages",
    channel_id: "aihubmix",
    protocol: "claude_messages",
    capability_contract_id: "claude-sonnet-5-adaptive",
    protocol_profile_id: "claude-messages-aihubmix-v1",
    cache_policy_id: "session-private-cache-policy-v1",
    continuation_policy_id: null,
    priority: 80,
    supports_continuation: false,
    supports_thinking: true,
  },
  {
    offering_id: "anthropic-messages-aihubmix",
    provider: "anthropic",
    model_pattern: "*",
    connection_id: "aihubmix_claude_messages",
    channel_id: "aihubmix",
    protocol: "claude_messages",
    capability_contract_id: "claude-sonnet-5-adaptive",
    protocol_profile_id: "claude-messages-aihubmix-v1",
    cache_policy_id: "session-private-cache-policy-v1",
    continuation_policy_id: null,
    priority: 80,
    supports_continuation: false,
    supports_thinking: true,
  },
  {
    offering_id: "moonshot-official",
    provider: "moonshot",
    model_pattern: "kimi-*",
    connection_id: "moonshot_official",
    channel_id: "moonshot_official",
    protocol: "moonshot_chat",
    capability_contract_id: "kimi-legacy-moonshot",
    protocol_profile_id: "moonshot-chat-v1",
    cache_policy_id: "session-private-cache-policy-v1",
    continuation_policy_id: null,
    priority: 80,
    supports_continuation: false,
    supports_thinking: true,
  },
];

export function canonicalCapabilityContract(
  contract: CapabilityContractSpec
): Record<string, unknown> {
  const contCanonical = canonicalContinuationContract(contract.continuation);
  return {
    contract_id: contract.contract_id,
    revision: contract.revision,
    input_modalities: [...contract.input_modalities],
    structured_output_mode: contract.structured_output_mode,
    think_levels: [...contract.think_levels],
    features: [...contract.features],
    cache: contract.cache,
    ...(contCanonical ? { continuation: contCanonical } : {}),
    ...(contract.metadata && Object.keys(contract.metadata).length > 0
      ? { metadata: contract.metadata }
      : {}),
  };
}

export function canonicalProtocolProfile(
  profile: ProtocolProfileSpec
): Record<string, unknown> {
  return {
    profile_id: profile.profile_id,
    revision: profile.revision,
    protocol: profile.protocol,
    supports_native_history_material_reuse:
      profile.supports_native_history_material_reuse,
    cache: profile.cache,
    ...(profile.continuation ? { continuation: profile.continuation } : {}),
    affinity_policy: profile.affinity_policy,
  };
}

export function canonicalOffering(offering: OfferingSpec): Record<string, unknown> {
  return {
    offering_id: offering.offering_id,
    provider: offering.provider,
    model_pattern: offering.model_pattern,
    connection_id: offering.connection_id,
    channel_id: offering.channel_id,
    protocol: offering.protocol,
    capability_contract_id: offering.capability_contract_id,
    protocol_profile_id: offering.protocol_profile_id,
    cache_policy_id: offering.cache_policy_id,
    ...(offering.continuation_policy_id
      ? { continuation_policy_id: offering.continuation_policy_id }
      : {}),
    priority: offering.priority,
    supports_continuation: offering.supports_continuation,
    supports_thinking: offering.supports_thinking,
  };
}

export function supportsRequirements(
  capability: CapabilityContractSpec,
  channelId: string,
  requirements?: {
    input_modalities?: string[];
    structured_output?: "none" | "post_validate" | "native";
    think_levels?: string[];
    required_features?: string[];
    channel_allowlist?: string[];
  }
): boolean {
  if (!requirements) return true;

  if (
    requirements.channel_allowlist &&
    requirements.channel_allowlist.length > 0 &&
    !requirements.channel_allowlist.includes(channelId)
  ) {
    return false;
  }

  for (const mod of requirements.input_modalities ?? []) {
    if (!capability.input_modalities.includes(mod)) {
      return false;
    }
  }

  if (
    requirements.structured_output === "native" &&
    capability.structured_output_mode !== "native"
  ) {
    return false;
  }

  for (const tl of requirements.think_levels ?? []) {
    if (!capability.think_levels.includes(tl.toLowerCase())) {
      return false;
    }
  }

  const availableFeatures = new Set(capability.features);
  if (
    capability.continuation?.verification_status === "verified" &&
    capability.continuation.anchor_class !== "none"
  ) {
    availableFeatures.add("stateful_continuation");
  }

  for (const feat of requirements.required_features ?? []) {
    if (!availableFeatures.has(feat)) {
      return false;
    }
  }

  return true;
}

export function validateControlPlane(): void {
  for (const offering of BUILTIN_OFFERINGS) {
    const conn = CONNECTIONS[offering.connection_id];
    if (!conn) {
      throw new Error(`Offering ${offering.offering_id} references unknown connection ${offering.connection_id}`);
    }
    const capability = CAPABILITY_CONTRACTS[offering.capability_contract_id];
    if (!capability) {
      throw new Error(
        `Offering ${offering.offering_id} references unknown capability ${offering.capability_contract_id}`
      );
    }
    const profile = PROTOCOL_PROFILES[offering.protocol_profile_id] ?? null;
    if (!profile) {
      throw new Error(
        `Offering ${offering.offering_id} references unknown profile ${offering.protocol_profile_id}`
      );
    }
    let policy: ContinuationPolicySpec | null = null;
    if (offering.continuation_policy_id) {
      policy = CONTINUATION_POLICIES[offering.continuation_policy_id] ?? null;
      if (!policy) {
        throw new Error(
          `Offering ${offering.offering_id} references unknown continuation_policy_id ${offering.continuation_policy_id}`
        );
      }
    }
    validateContinuationCrossContract({
      connection: conn,
      capability,
      profile,
      policy,
    });
  }
}

// Execute compile-time cross-contract gates immediately on Control Plane initialization.
validateControlPlane();

function wildcardMatch(pattern: string, value: string): boolean {
  const escaped = pattern
    .toLowerCase()
    .replace(/[.+^${}()|[\]\\]/g, "\\$&")
    .replace(/\*/g, ".*")
    .replace(/\?/g, ".");
  return new RegExp(`^${escaped}$`).test(value.toLowerCase());
}

export const CONTROL_PLANE_HASH = stableHash({
  offerings: BUILTIN_OFFERINGS.map(canonicalOffering),
  capabilities: Object.values(CAPABILITY_CONTRACTS).map(canonicalCapabilityContract),
  profiles: Object.values(PROTOCOL_PROFILES).map(canonicalProtocolProfile),
});

export const ROUTE_CATALOG_HASH = stableHash({
  revision: config.routeRevision,
  offerings: BUILTIN_OFFERINGS.map(canonicalOffering),
});

export function resolveRoute(
  provider: string,
  model: string,
  preferredProtocol?: string,
  requirements?: {
    input_modalities?: string[];
    structured_output?: "none" | "post_validate" | "native";
    think_levels?: string[];
    required_features?: string[];
    channel_allowlist?: string[];
  }
): RouteBinding {
  const normalizedProvider = provider.trim().toLowerCase();
  const candidates = BUILTIN_OFFERINGS.filter((item) => {
    if (item.provider !== normalizedProvider) return false;
    if (!wildcardMatch(item.model_pattern, model.trim())) return false;
    const cap = CAPABILITY_CONTRACTS[item.capability_contract_id];
    return cap ? supportsRequirements(cap, item.channel_id, requirements) : true;
  }).sort((a, b) => {
    if (preferredProtocol) {
      if (a.protocol === preferredProtocol && b.protocol !== preferredProtocol) return -1;
      if (b.protocol === preferredProtocol && a.protocol !== preferredProtocol) return 1;
    }
    return b.priority - a.priority;
  });

  const chosen = candidates[0] ?? {
    offering_id: `${normalizedProvider}-dynamic-fallback`,
    provider: normalizedProvider,
    model_pattern: "*",
    connection_id: `${normalizedProvider}_default`,
    channel_id: normalizedProvider === "gemini" ? "google_official" : "aihubmix",
    protocol:
      normalizedProvider === "gemini"
        ? "gemini_native"
        : normalizedProvider === "claude" || normalizedProvider === "anthropic"
          ? "claude_messages"
          : normalizedProvider === "moonshot"
            ? "moonshot_chat"
            : "chat_completions",
    capability_contract_id: `${normalizedProvider}-default-capability`,
    protocol_profile_id: `${normalizedProvider}-default-profile`,
    cache_policy_id: "session-private-cache-policy-v1",
    continuation_policy_id: null,
    priority: 10,
    supports_continuation: false,
    supports_thinking: true,
  };

  const connection = CONNECTIONS[chosen.connection_id] ?? {
    connection_id: chosen.connection_id,
    channel_id: chosen.channel_id,
    protocol: chosen.protocol,
    account_scope_id: `${chosen.channel_id}:default`,
    base_url: "",
    auth_type: "bearer",
    zero_data_retention: false,
  };

  const capability = CAPABILITY_CONTRACTS[chosen.capability_contract_id];
  const profile = PROTOCOL_PROFILES[chosen.protocol_profile_id];
  const policy = chosen.continuation_policy_id
    ? CONTINUATION_POLICIES[chosen.continuation_policy_id]
    : undefined;

  const accountScopeId = connection.account_scope_id;
  const accountScopeHash = stableHash({ account_scope_id: accountScopeId });
  const connectionSpecHash = stableHash({
    connection_id: connection.connection_id,
    channel_id: connection.channel_id,
    protocol: connection.protocol,
    account_scope_id: connection.account_scope_id,
    base_url: connection.base_url,
    auth_type: connection.auth_type,
    zero_data_retention: connection.zero_data_retention,
  });
  const protocolProfileHash = profile
    ? stableHash(canonicalProtocolProfile(profile))
    : stableHash({ profile_id: chosen.protocol_profile_id, protocol: chosen.protocol });

  const contContractCanonical = canonicalContinuationContract(capability?.continuation);
  const contPolicyCanonical = canonicalContinuationPolicy(policy);

  const continuationContractHash = contContractCanonical
    ? stableHash(contContractCanonical)
    : undefined;
  const continuationPolicyHash = contPolicyCanonical
    ? stableHash(contPolicyCanonical)
    : undefined;

  const bindingBase: Record<string, unknown> = {
    provider: normalizedProvider,
    model: model.trim(),
    connection_id: chosen.connection_id,
    channel_id: chosen.channel_id,
    protocol: chosen.protocol,
    offering_id: chosen.offering_id,
    capability_contract_id: chosen.capability_contract_id,
    protocol_profile_id: chosen.protocol_profile_id,
    cache_policy_id: chosen.cache_policy_id,
    account_scope_id: accountScopeId,
    account_scope_hash: accountScopeHash,
    connection_spec_hash: connectionSpecHash,
    protocol_profile_hash: protocolProfileHash,
    zero_data_retention: connection.zero_data_retention,
    route_revision: config.routeRevision,
    capability_revision: CAPABILITY_PROFILE_REVISION,
    control_plane_hash: CONTROL_PLANE_HASH,
    cache_contract_hash: stableHash({ policy: chosen.cache_policy_id }),
    execution_pool: config.executionPool,
    upstream_model: model.trim(),
    ...(profile?.continuation
      ? { protocol_profile_continuation: profile.continuation }
      : {}),
    ...(capability?.continuation && contContractCanonical
      ? {
          continuation_contract_hash: continuationContractHash,
          continuation_contract: capability.continuation,
        }
      : {}),
    ...(policy && contPolicyCanonical
      ? {
          continuation_policy_id: policy.policy_id,
          continuation_policy_hash: continuationPolicyHash,
          continuation_policy: policy,
        }
      : {}),
  };

  return {
    ...(bindingBase as unknown as Omit<RouteBinding, "route_binding_hash">),
    route_binding_hash: stableHash(bindingBase),
  };
}

export function getRegisteredProviders(): string[] {
  return Array.from(new Set(BUILTIN_OFFERINGS.map((o) => o.provider)));
}

export function getConfigurationReport(): Record<string, unknown> {
  return {
    status: "ready",
    persistence: "in_memory",
    control_plane_revision: config.modelControlPlaneRevision,
    control_plane_hash: CONTROL_PLANE_HASH,
    route_revision: config.routeRevision,
    route_catalog_hash: ROUTE_CATALOG_HASH,
    capability_revision: CAPABILITY_PROFILE_REVISION,
    configured_credentials: {
      relay_api_token: Boolean(config.relayApiToken),
      gemini: Boolean(config.geminiApiKey),
      aihubmix: Boolean(config.aihubmixApiKey),
      moonshot: Boolean(config.moonshotApiKey),
    },
    offerings_count: BUILTIN_OFFERINGS.length,
    continuation_policies_count: Object.keys(CONTINUATION_POLICIES).length,
    providers: getRegisteredProviders(),
  };
}
