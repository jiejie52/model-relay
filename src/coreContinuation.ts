import { stableHash } from "./store.js";
import type {
  EffectiveMaterialPlacementPlan,
  HistoryTurn,
  MaterialOccurrenceRef,
  TurnPrefixChainEntry,
} from "./types.js";

export const LEGACY_CONTINUATION_HEAD = "relay-continuation-head/2";
export const INTERACTIONS_HISTORY_HEAD = "relay-continuation-head/3";
export const CONTRACT_DRIVEN_HEAD = "relay-continuation-head/4";

export const FORBIDDEN_IDENTITY_KEYS = new Set([
  "previous_interaction_id",
  "previous_response_id",
  "selected_anchor_id",
  "active_anchor_id",
  "parent_anchor_id",
  "upstream_handle",
  "_relay_continuation_execution",
]);

function normalizeToolsForHead(rawTools: unknown): unknown {
  if (!Array.isArray(rawTools) || rawTools.length === 0) {
    return null;
  }
  return rawTools.map((tool) => {
    if (tool && typeof tool === "object" && !Array.isArray(tool)) {
      return tool;
    }
    return tool;
  });
}

export interface ContinuationHeadSnapshot {
  session_id: string;
  tenant_id: string;
  conversation_hash: string;
  connection_id: string;
  account_scope_id: string;
  connection_spec_hash: string;
  protocol_profile_hash: string;
  model: string;
  upstream_model: string;
  protocol: string;
  instructions?: string | null;
  tools?: unknown[];
  provider_payload?: Record<string, unknown>;
}

/**
 * Computes the parent head fingerprint orthogonal to per-turn sampling,
 * thinking configuration, and structured output schema choices.
 */
export function continuationHead(
  snapshot: ContinuationHeadSnapshot,
  options?: {
    headFormat?: string;
    headInvariants?: string[];
  }
): string {
  const headFormat = options?.headFormat ?? CONTRACT_DRIVEN_HEAD;
  const headInvariants = options?.headInvariants ?? [
    "instructions",
    "tools",
    "account_scope_id",
    "model",
  ];

  if (headFormat === LEGACY_CONTINUATION_HEAD || headFormat === INTERACTIONS_HISTORY_HEAD) {
    const legacyPayload: Record<string, unknown> = {
      format: headFormat,
      session_id: snapshot.session_id,
      tenant_id: snapshot.tenant_id,
      conversation_hash: snapshot.conversation_hash,
      connection_id: snapshot.connection_id,
      account_scope_id: snapshot.account_scope_id,
      connection_spec_hash: snapshot.connection_spec_hash,
      protocol_profile_hash: snapshot.protocol_profile_hash,
      model: snapshot.model,
      upstream_model: snapshot.upstream_model,
      protocol: snapshot.protocol,
    };
    if (headFormat === LEGACY_CONTINUATION_HEAD) {
      const inst = snapshot.instructions;
      legacyPayload.instructions = typeof inst === "string" ? inst.trim() : inst ?? null;
      legacyPayload.tools = normalizeToolsForHead(
        snapshot.tools ?? snapshot.provider_payload?.tools
      );
    }
    return stableHash(legacyPayload);
  }

  if (headFormat !== CONTRACT_DRIVEN_HEAD) {
    throw new Error(`Unsupported continuation head format: ${headFormat}`);
  }

  const payload: Record<string, unknown> = {
    format: CONTRACT_DRIVEN_HEAD,
    session_id: snapshot.session_id,
    tenant_id: snapshot.tenant_id,
    conversation_hash: snapshot.conversation_hash,
    connection_id: snapshot.connection_id,
    account_scope_id: snapshot.account_scope_id,
    connection_spec_hash: snapshot.connection_spec_hash,
    protocol_profile_hash: snapshot.protocol_profile_hash,
    model: snapshot.model,
    upstream_model: snapshot.upstream_model,
    protocol: snapshot.protocol,
  };

  if (headInvariants.includes("instructions")) {
    const inst = snapshot.instructions;
    payload.instructions = typeof inst === "string" ? inst.trim() : inst ?? null;
  }

  if (headInvariants.includes("tools")) {
    const tools = snapshot.tools ?? snapshot.provider_payload?.tools;
    payload.tools = normalizeToolsForHead(tools);
  }

  return stableHash(payload);
}

export function canonicalHistoryTurnDigest(turn: HistoryTurn): string {
  return stableHash({
    format: "relay-frozen-turn/1",
    role: turn.role,
    text: turn.text,
    material_ids: turn.material_ids ?? [],
    material_occurrences: turn.material_occurrences ?? [],
    native_steps: turn.native_steps ?? [],
  });
}

export function buildTurnPrefixChain(
  parentHeadFingerprint: string,
  frozenHistory: HistoryTurn[]
): TurnPrefixChainEntry[] {
  const chain: TurnPrefixChainEntry[] = [];
  let prevChainDigest = parentHeadFingerprint;
  for (let i = 0; i < frozenHistory.length; i++) {
    const turnDigest = canonicalHistoryTurnDigest(frozenHistory[i]);
    const turnChainDigest = stableHash({
      parent: prevChainDigest,
      turn_index: i,
      turn_digest: turnDigest,
    });
    chain.push({
      turn_index: i,
      turn_digest: turnDigest,
      turn_chain_digest: turnChainDigest,
    });
    prevChainDigest = turnChainDigest;
  }
  return chain;
}

/**
 * Proves that candidate anchor covered material bindings union delta turn occurrences
 * fully cover all historical_only_material_ids required by the placement plan.
 */
export function verifyParentChainMaterialCoverage(params: {
  placementPlan: EffectiveMaterialPlacementPlan;
  anchorMaterialBindings: MaterialOccurrenceRef[];
  deltaTurns: HistoryTurn[];
}): { ok: boolean; reason: string | null } {
  const required = params.placementPlan.historical_only_material_ids;
  if (required.length === 0) {
    return { ok: true, reason: null };
  }

  const coveredIds = new Set<string>();
  for (const ref of params.anchorMaterialBindings) {
    coveredIds.add(ref.material_id);
  }
  for (const turn of params.deltaTurns) {
    for (const matId of turn.material_ids ?? []) {
      coveredIds.add(matId);
    }
    for (const occ of turn.material_occurrences ?? []) {
      coveredIds.add(occ.material_id);
    }
  }

  const missing = required.filter((id) => !coveredIds.has(id));
  if (missing.length > 0) {
    return {
      ok: false,
      reason: `material_coverage_gap:${missing.join(",")}`,
    };
  }
  return { ok: true, reason: null };
}

function scanForbiddenKeys(value: unknown, path = ""): string | null {
  if (!value || typeof value !== "object") return null;
  if (Array.isArray(value)) {
    for (let i = 0; i < value.length; i++) {
      const found = scanForbiddenKeys(value[i], `${path}[${i}]`);
      if (found) return found;
    }
    return null;
  }
  const obj = value as Record<string, unknown>;
  for (const [k, v] of Object.entries(obj)) {
    if (FORBIDDEN_IDENTITY_KEYS.has(k) && v !== null && v !== undefined) {
      return path ? `${path}.${k}` : k;
    }
    const nested = scanForbiddenKeys(v, path ? `${path}.${k}` : k);
    if (nested) return nested;
  }
  return null;
}

/**
 * Computes request_identity hash and enforces that no dynamic continuation handles
 * or runtime anchor IDs ever pollute logical request idempotency.
 */
export function computeCleanRequestIdentity(
  identityFields: Record<string, unknown>,
  continuationPlan: Record<string, unknown> | null,
  contractHash?: string | null,
  policyHash?: string | null
): string {
  const directForbidden = scanForbiddenKeys(identityFields);
  if (directForbidden) {
    throw new Error(
      `Dynamic continuation handle must never enter request_identity (${directForbidden})`
    );
  }

  const payload: Record<string, unknown> = { ...identityFields };

  if (continuationPlan && continuationPlan.schema_version === "relay-continuation-plan/2") {
    const planForbidden = scanForbiddenKeys(continuationPlan);
    if (planForbidden) {
      throw new Error(
        `Dynamic continuation handle must never enter continuation_plan_hash (${planForbidden})`
      );
    }
    payload.continuation_contract_hash = contractHash ?? null;
    payload.continuation_policy_hash = policyHash ?? null;
    payload.continuation_plan_hash = continuationPlan.plan_hash ?? null;
  }

  return stableHash(payload);
}

/**
 * Canonical post-validation for structured_output.mode = "json_schema".
 * Note: json_object does NOT enforce JSON parsing per UPGRADE_10.1.4.
 */
export function validateJsonAgainstCanonicalSchema(
  rawText: string,
  schema: Record<string, unknown>
): { ok: true; parsed: unknown } | { ok: false; reason: string } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(rawText);
  } catch (err) {
    return {
      ok: false,
      reason: `Output is not valid JSON for json_schema: ${err instanceof Error ? err.message : String(err)}`,
    };
  }

  const err = checkSchemaNode(parsed, schema, "$");
  if (err) {
    return { ok: false, reason: err };
  }
  return { ok: true, parsed };
}

function checkSchemaNode(
  value: unknown,
  schema: Record<string, unknown>,
  path: string
): string | null {
  const expectedType = schema.type;
  if (expectedType !== undefined) {
    const allowed = Array.isArray(expectedType) ? expectedType : [expectedType];
    const matches = allowed.some((t) => matchesJsonType(value, String(t)));
    if (!matches) {
      return `${path}: expected type ${allowed.join("|")}, got ${value === null ? "null" : Array.isArray(value) ? "array" : typeof value}`;
    }
  }

  if (Array.isArray(schema.enum) && !schema.enum.some((e) => stableHash(e) === stableHash(value))) {
    return `${path}: value is not in enum`;
  }

  if (value && typeof value === "object" && !Array.isArray(value)) {
    const obj = value as Record<string, unknown>;
    if (Array.isArray(schema.required)) {
      for (const reqKey of schema.required) {
        if (typeof reqKey === "string" && !(reqKey in obj)) {
          return `${path}: missing required property '${reqKey}'`;
        }
      }
    }
    const props =
      schema.properties && typeof schema.properties === "object"
        ? (schema.properties as Record<string, unknown>)
        : {};
    for (const [k, v] of Object.entries(obj)) {
      const propSchema = props[k];
      if (propSchema && typeof propSchema === "object" && !Array.isArray(propSchema)) {
        const childErr = checkSchemaNode(v, propSchema as Record<string, unknown>, `${path}.${k}`);
        if (childErr) return childErr;
      } else if (schema.additionalProperties === false) {
        return `${path}: unexpected additional property '${k}'`;
      }
    }
  }

  if (Array.isArray(value) && schema.items && typeof schema.items === "object" && !Array.isArray(schema.items)) {
    const itemSchema = schema.items as Record<string, unknown>;
    for (let i = 0; i < value.length; i++) {
      const childErr = checkSchemaNode(value[i], itemSchema, `${path}[${i}]`);
      if (childErr) return childErr;
    }
  }

  return null;
}

function matchesJsonType(value: unknown, typeName: string): boolean {
  switch (typeName) {
    case "null":
      return value === null;
    case "boolean":
      return typeof value === "boolean";
    case "string":
      return typeof value === "string";
    case "number":
      return typeof value === "number" && Number.isFinite(value);
    case "integer":
      return typeof value === "number" && Number.isInteger(value);
    case "array":
      return Array.isArray(value);
    case "object":
      return value !== null && typeof value === "object" && !Array.isArray(value);
    default:
      return true;
  }
}
