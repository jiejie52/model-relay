import crypto from "node:crypto";
import { Router, type Request, type Response } from "express";
import multer from "multer";
import { config } from "../config.js";
import { ContinuationDecisionError } from "../continuationContracts.js";
import { resolveRoute } from "../controlPlane.js";
import { computeCleanRequestIdentity } from "../coreContinuation.js";
import {
  ContextPlanner,
  ContinuationIntentPlanner,
  ResultCommitter,
} from "../executionPlane.js";
import { extractOwnerHeaders, requireOwnerHeaders, requireRelayAuth } from "../middleware/auth.js";
import { executeProviderCall } from "../providers.js";
import {
  AtomicDispatchSealer,
  ContinuationBinder,
  isUpstreamAnchorRejected,
  MaterialBinder,
  PhysicalContextProjector,
  type RuntimeExecutionState,
} from "../runtime.js";
import { stableHash, store } from "../store.js";
import {
  MaterialCreateJSONSchema,
  SessionCreateRequestSchema,
  SessionRequestCreateSchema,
  type ActualExecutionFact,
  type ContextPlan,
  type ContinuationIntentPlan,
  type MaterialRecord,
  type RequestRecord,
  type SessionRecord,
} from "../types.js";

const upload = multer({
  storage: multer.memoryStorage(),
  limits: { fileSize: config.materialIngressMaxBytes },
});

function formatMaterialResponse(row: MaterialRecord) {
  return {
    schema_version: "relay-material/2.2",
    material_id: row.id,
    status: row.status,
    filename: row.filename,
    content_type: row.content_type,
    size: row.size_bytes,
    size_bytes: row.size_bytes,
    sha256: row.sha256,
    provider: row.provider,
    model: row.model,
    purpose: row.purpose,
    route_revision: row.route_revision,
    durability: row.durability,
    ready_for: row.provider ? [row.provider] : [],
    fallback: {
      stored: true,
      object_ref: `internal://objects/${row.object_id}`,
      storage_id: row.storage_id,
    },
    provider_binding: null,
    object_id: row.object_id,
    storage_id: row.storage_id,
    source_ref: row.source_ref,
    parent_material_id: row.parent_material_id,
    ordinal: row.ordinal,
    created_at: row.created_at,
  };
}

export function formatSessionResponse(row: SessionRecord) {
  return {
    schema_version: "relay-session/2.1",
    session_id: row.id,
    tenant_id: row.tenant_id,
    conversation_hash: row.conversation_hash,
    provider: row.provider,
    model: row.model,
    context_policy: row.context_policy,
    material_ids: row.material_ids,
    history_version: row.history_version,
    route_pin_state: row.route_pin_state,
    supply_group_id: row.supply_group_id,
    history_mode: row.history_mode,
    sticky_scope: row.sticky_scope,
    active_request_id: row.active_request_id,
    execution_pool: row.execution_pool,
    route_revision: row.route.route_revision,
    capability_revision: row.route.capability_revision,
    offering_id: row.route.offering_id,
    channel_id: row.route.channel_id,
    protocol: row.route.protocol,
    capability_contract_id: row.route.capability_contract_id,
    control_plane_hash: row.route.control_plane_hash,
    protocol_profile_id: row.route.protocol_profile_id,
    cache_policy_id: row.route.cache_policy_id,
    cache_contract_hash: row.route.cache_contract_hash,
    ...(row.route.continuation_contract_hash
      ? { continuation_contract_hash: row.route.continuation_contract_hash }
      : {}),
    ...(row.route.continuation_policy_id
      ? { continuation_policy_id: row.route.continuation_policy_id }
      : {}),
    created_at: row.created_at,
    expires_at: row.expires_at,
  };
}

export function formatRequestEnvelope(row: RequestRecord) {
  return {
    schema_version: "relay-session/2.1",
    session_id: row.session_id,
    request_id: row.id,
    job_id: row.job_id,
    execution: { mode: row.execution_mode },
    status: row.status,
    provider_dispatch_state: row.provider_dispatch_state,
    history_version: row.history_version,
    result: row.result,
    error: row.error,
    cache: row.cache,
    continuation: row.continuation,
    poll_after_seconds:
      row.status === "queued" || row.status === "leased" || row.status === "running"
        ? 5
        : null,
  };
}

export async function createSessionCore(
  rawBody: unknown,
  idempotencyKey: string
): Promise<{
  status: number;
  body: Record<string, unknown>;
}> {
  const parsed = SessionCreateRequestSchema.safeParse(rawBody);
  if (!parsed.success) {
    return { status: 422, body: { detail: parsed.error.issues } };
  }
  const body = parsed.data;
  const contextPolicy =
    typeof body.context_policy === "string" ? body.context_policy : "conversation";
  const historyMode =
    typeof body.context_policy === "object"
      ? body.context_policy.history_mode
      : "cumulative";
  const stickyScope =
    typeof body.context_policy === "object"
      ? body.context_policy.sticky_scope
      : "session";

  const sessionIntent = {
    tenant_id: body.tenant_id,
    conversation_hash: body.conversation_hash,
    provider: body.provider,
    model: body.model,
    context_policy: body.context_policy,
    material_ids: body.material_ids,
    execution_pool: body.execution_pool || config.executionPool,
    capability_requirements: body.capability_requirements,
    metadata: body.metadata,
  };
  const sessionHash = stableHash(sessionIntent);
  const idemKey = `${body.tenant_id}:${idempotencyKey}`;
  const existingId = store.sessionIdempotency.get(idemKey);
  if (existingId) {
    const existing = store.sessions.get(existingId);
    if (existing) {
      if (existing.session_hash !== sessionHash) {
        return {
          status: 409,
          body: {
            detail: "Idempotency-Key was already used for a different Session",
          },
        };
      }
      return { status: 201, body: formatSessionResponse(existing) };
    }
  }

  for (const matId of body.material_ids) {
    const mat = store.materials.get(matId);
    if (
      !mat ||
      mat.tenant_id !== body.tenant_id ||
      mat.conversation_hash !== body.conversation_hash
    ) {
      return {
        status: 409,
        body: { detail: `Material is not usable: ${matId}` },
      };
    }
  }

  const route = resolveRoute(
    body.provider,
    body.model,
    undefined,
    body.capability_requirements
  );
  const now = new Date();
  const expiresAt = new Date(now.getTime() + config.sessionTtlSeconds * 1000);
  const id = crypto.randomUUID();
  const row: SessionRecord = {
    id,
    tenant_id: body.tenant_id,
    conversation_hash: body.conversation_hash,
    provider: route.provider,
    model: route.model,
    connection_id: route.connection_id,
    context_policy: contextPolicy,
    history_mode: historyMode,
    sticky_scope: stickyScope,
    material_ids: body.material_ids,
    history_version: 0,
    route_pin_state: "pinned",
    supply_group_id: null,
    active_request_id: null,
    execution_pool: route.execution_pool,
    idempotency_key: idempotencyKey,
    session_hash: sessionHash,
    route,
    committed_material_ledger: [],
    metadata: body.metadata,
    created_at: now.toISOString(),
    expires_at: expiresAt.toISOString(),
  };

  store.sessions.set(id, row);
  store.sessionIdempotency.set(idemKey, id);
  return { status: 201, body: formatSessionResponse(row) };
}

async function runRequestExecution(
  session: SessionRecord,
  reqRow: RequestRecord,
  params: {
    input: unknown;
    instructions: string | null;
    tools: unknown[];
    contextPlan: ContextPlan;
    continuationPlan: ContinuationIntentPlan;
    currentMaterials: MaterialRecord[];
    thinkLevel: string | null;
    options: Record<string, unknown>;
    structuredOutput: Record<string, unknown>;
    cacheMode: string;
    cacheMechanism: string;
  }
): Promise<void> {
  reqRow.status = "running";

  const runtimeState: RuntimeExecutionState = {
    session,
    request: reqRow,
    contextPlan: params.contextPlan,
    continuationPlan: params.continuationPlan,
    currentMaterials: params.currentMaterials,
    liveBindings: new Map(),
    historicalPinnedBindings: [],
    continuationBinding: null,
    preparedPayloadDigest: "",
    sealedBundle: null,
    wireAffinityEpoch: null,
  };

  // Fixed Runtime Sequence: MaterialBinder -> ContinuationBinder -> PhysicalContextProjector -> AtomicDispatchSealer
  MaterialBinder.bindForPlan(runtimeState);
  ContinuationBinder.bindForContext(runtimeState);
  PhysicalContextProjector.project(runtimeState);

  let sealRes = AtomicDispatchSealer.sealBeforeDispatch(runtimeState);
  if (
    !sealRes.ok &&
    sealRes.code === "CONTINUATION_PARENT_NO_LONGER_USABLE" &&
    params.continuationPlan.allow_stateless_fallback
  ) {
    // Pre-seal lossless downgrade while still in 'not_sent'
    PhysicalContextProjector.reprojectForContinuation(runtimeState);
    sealRes = AtomicDispatchSealer.sealBeforeDispatch(runtimeState);
  }

  if (!sealRes.ok) {
    reqRow.error = {
      source: "relay",
      code: sealRes.code,
      message: sealRes.message,
    };
    store.releaseContinuationAfterRequest({
      session,
      request: reqRow,
      invalidateAnchorId: null,
      invalidationReason: null,
    });
    return;
  }

  const binding = runtimeState.continuationBinding!;
  const visibleHistory = params.contextPlan.visible_history;
  const deltaHistory =
    binding.effective_mode === "disabled"
      ? visibleHistory
      : visibleHistory.slice(
          binding.delta_start_turn_index,
          binding.delta_end_turn_index
        );
  const omittedHistory =
    binding.effective_mode === "stateful_anchor_delta"
      ? visibleHistory.slice(0, binding.delta_start_turn_index)
      : [];

  const execResult = await executeProviderCall({
    route: session.route,
    input: params.input,
    instructions: params.instructions,
    tools: params.tools,
    history: visibleHistory,
    deltaHistory,
    omittedHistory,
    materials: params.currentMaterials,
    thinkLevel: params.thinkLevel,
    options: params.options,
    structuredOutput: params.structuredOutput,
    previousInteractionId:
      binding.effective_mode === "stateful_anchor_delta"
        ? binding.upstream_handle
        : null,
    storeUpstreamState: binding.store_upstream_state,
    requiresThoughtSignatureOnReplay:
      session.route.continuation_contract?.requires_thought_signature_on_replay ??
      true,
    cacheMode: params.cacheMode,
    cacheMechanism: params.cacheMechanism,
  });

  if (!execResult.ok) {
    const anchorRejected = isUpstreamAnchorRejected({
      upstreamHttpStatus: execResult.error.upstream_http_status,
      message: execResult.error.message,
      bodyText: execResult.error.body_text,
      binding,
      profileContinuation: session.route.protocol_profile_continuation,
    });

    reqRow.error = execResult.error;
    reqRow.raw_error_bytes = execResult.rawBytes ?? null;
    reqRow.raw_error_content_type = execResult.rawContentType ?? null;
    store.releaseContinuationAfterRequest({
      session,
      request: reqRow,
      invalidateAnchorId: anchorRejected ? binding.selected_anchor_id : null,
      invalidationReason: anchorRejected
        ? `upstream_rejected_${execResult.error.upstream_http_status ?? "error"}`
        : null,
    });
    return;
  }

  // Archive execution fact and cross into 'result_stored' before running post-validation & atomic commit
  const fact: ActualExecutionFact = {
    schema_version: "relay-execution-fact/2",
    request_id: reqRow.id,
    session_id: session.id,
    seal_hash: sealRes.bundle.seal_hash,
    continuation_execution_binding: binding,
    upstream_anchor_ref: execResult.interactionId,
    upstream_state_stored: execResult.upstreamStateStored,
    previous_anchor_ref: execResult.previousAnchorRef,
    native_steps: execResult.nativeSteps,
    text: execResult.text,
    raw_text: execResult.rawText,
    model: execResult.model,
    usage: execResult.usage,
    affinity_epoch: runtimeState.wireAffinityEpoch,
    created_at: new Date().toISOString(),
  };
  store.executionFacts.set(reqRow.id, fact);
  reqRow.provider_dispatch_state = "result_stored";

  ResultCommitter.finalize({
    session,
    request: reqRow,
    contextPlan: params.contextPlan,
    continuationPlan: params.continuationPlan,
    fact,
    input: params.input,
    currentMaterials: params.currentMaterials,
    liveBindings: runtimeState.liveBindings,
    structuredOutput: params.structuredOutput,
    cacheMode: params.cacheMode,
    cacheMechanism: params.cacheMechanism,
  });
}

export async function createSessionRequestCore(
  sessionId: string,
  rawBody: unknown,
  idempotencyKey: string,
  owner: { tenantId: string; conversationHash: string }
): Promise<{ status: number; body: Record<string, unknown> }> {
  const session = store.sessions.get(sessionId);
  if (
    !session ||
    session.tenant_id !== owner.tenantId ||
    session.conversation_hash !== owner.conversationHash
  ) {
    return { status: 404, body: { detail: "Session not found" } };
  }

  const parsed = SessionRequestCreateSchema.safeParse(rawBody);
  if (!parsed.success) {
    return { status: 422, body: { detail: parsed.error.issues } };
  }
  const body = parsed.data;

  const requestPolicy = body.context_policy || session.context_policy;
  if (session.context_policy === "explicit" && requestPolicy !== "explicit") {
    return {
      status: 422,
      body: {
        detail: {
          code: "CONTEXT_POLICY_OVERRIDE_NOT_ALLOWED",
          message: "An explicit Session cannot inherit Session materials or history",
        },
      },
    };
  }

  const tools = body.tools ?? (Array.isArray(body.provider_payload.tools) ? body.provider_payload.tools : []);
  const supportsNativeHistoryReuse =
    session.route.protocol === "gemini_interactions" ||
    session.route.protocol === "gemini_native";

  // Execution Plane Planning (pure, zero live anchor ledger reads)
  const fullHistory = store.getHistory(session.id);
  const contextPlan = ContextPlanner.buildContextPlan({
    session,
    effectivePolicy: requestPolicy,
    instructions: body.instructions ?? null,
    tools,
    explicitMaterialIds: body.material_ids,
    providerPayload: body.provider_payload,
    fullHistory,
    supportsNativeHistoryReuse,
  });

  let continuationPlan: ContinuationIntentPlan;
  try {
    continuationPlan = ContinuationIntentPlanner.planFrozenContext({
      session,
      contextPlan,
      requestedContinuationMode: body.continuation_mode,
      requestedCacheMode: body.requested_cache_mode,
      requestedCacheMechanism: body.requested_cache_mechanism,
    });
  } catch (err) {
    if (err instanceof ContinuationDecisionError) {
      return {
        status: err.httpStatus,
        body: { detail: { code: err.code, message: err.message } },
      };
    }
    throw err;
  }

  const publicIntentHash = computeCleanRequestIdentity(
    {
      input: body.input,
      instructions: body.instructions ?? null,
      tools,
      material_ids: body.material_ids,
      context_policy: body.context_policy ?? null,
      think_level: body.think_level ?? null,
      think_level_policy: body.think_level_policy,
      execution: body.execution,
      structured_output: body.structured_output,
      options: body.options,
      requested_cache_mode: body.requested_cache_mode,
      requested_cache_mechanism: body.requested_cache_mechanism,
      placement_digest: contextPlan.placement_plan.plan_digest,
      expected_history_version: session.history_version,
      metadata: body.metadata,
    },
    continuationPlan as unknown as Record<string, unknown>,
    session.route.continuation_contract_hash ?? null,
    session.route.continuation_policy_hash ?? null
  );

  const idemKey = `${sessionId}:${idempotencyKey}`;
  const existingId = store.requestIdempotency.get(idemKey);
  if (existingId) {
    const existing = store.requests.get(existingId);
    if (existing) {
      if (existing.caller_intent_hash !== publicIntentHash) {
        return { status: 409, body: { detail: { code: "IDEMPOTENCY_CONFLICT" } } };
      }

      // Support commit-only recovery when POST replays a request stuck at 'result_stored'
      if (existing.provider_dispatch_state === "result_stored") {
        const currentMats: MaterialRecord[] = [];
        for (const matId of contextPlan.placement_plan.current_effective_material_ids) {
          const mat = store.materials.get(matId);
          if (mat) currentMats.push(mat);
        }
        const liveBindings = new Map();
        for (const mat of currentMats) {
          liveBindings.set(
            mat.id,
            store.getOrCreateMaterialBinding(
              mat,
              session.route.account_scope_id,
              session.route.connection_id
            )
          );
        }
        ResultCommitter.recoverAndCommit({
          session,
          request: existing,
          contextPlan,
          continuationPlan,
          input: body.input,
          currentMaterials: currentMats,
          liveBindings,
          structuredOutput: body.structured_output,
          cacheMode: body.requested_cache_mode,
          cacheMechanism: body.requested_cache_mechanism,
        });
      }

      return { status: 200, body: formatRequestEnvelope(existing) };
    }
  }

  if (session.active_request_id) {
    return {
      status: 409,
      body: {
        detail: {
          code: "SESSION_BUSY",
          message: "Session already has an active request in flight",
        },
      },
    };
  }

  // Validate all effective materials belong to the same owner
  const allReferencedMaterialIds = Array.from(
    new Set([
      ...contextPlan.placement_plan.current_effective_material_ids,
      ...contextPlan.placement_plan.historical_only_material_ids,
    ])
  );
  const currentMaterials: MaterialRecord[] = [];
  for (const matId of allReferencedMaterialIds) {
    const mat = store.materials.get(matId);
    if (
      !mat ||
      mat.tenant_id !== owner.tenantId ||
      mat.conversation_hash !== owner.conversationHash
    ) {
      return { status: 409, body: { detail: `Material is not usable: ${matId}` } };
    }
    if (contextPlan.placement_plan.current_effective_material_ids.includes(matId)) {
      currentMaterials.push(mat);
    }
  }

  const continuationPlanObjectId = store.putJsonArtifact(
    continuationPlan as unknown as Record<string, unknown>
  );

  const requestId = crypto.randomUUID();
  const reqRow: RequestRecord = {
    id: requestId,
    session_id: sessionId,
    tenant_id: owner.tenantId,
    conversation_hash: owner.conversationHash,
    job_id: body.execution.mode === "async" ? crypto.randomUUID() : null,
    idempotency_key: idempotencyKey,
    caller_intent_hash: publicIntentHash,
    execution_mode: body.execution.mode,
    status: "queued",
    provider_dispatch_state: "not_sent",
    fence_owner: "inline_api",
    fence_epoch: 1,
    history_version: session.history_version,
    expected_history_version: session.history_version,
    continuation_plan_object_id: continuationPlanObjectId,
    continuation_plan_hash: continuationPlan.plan_hash,
    sealed_continuation_binding: null,
    prepared_payload_hash: null,
    produced_anchor_id: null,
    result: null,
    error: null,
    cache: null,
    continuation: null,
    raw_error_bytes: null,
    raw_error_content_type: null,
    created_at: new Date().toISOString(),
    completed_at: null,
  };

  store.requests.set(requestId, reqRow);
  store.requestIdempotency.set(idemKey, requestId);
  session.active_request_id = requestId;

  const execParams = {
    input: body.input,
    instructions: body.instructions ?? null,
    tools,
    contextPlan,
    continuationPlan,
    currentMaterials,
    thinkLevel: body.think_level ?? null,
    options: body.options,
    structuredOutput: body.structured_output,
    cacheMode: body.requested_cache_mode,
    cacheMechanism: body.requested_cache_mechanism,
  };

  if (body.execution.mode === "sync") {
    await runRequestExecution(session, reqRow, execParams);
  } else {
    setImmediate(() => {
      void runRequestExecution(session, reqRow, execParams);
    });
  }

  return { status: 200, body: formatRequestEnvelope(reqRow) };
}

export const v2Router = Router();
v2Router.use(requireRelayAuth);

v2Router.post(
  "/materials",
  upload.single("file"),
  async (req: Request, res: Response): Promise<void> => {
    const idempotencyKey = req.header("idempotency-key")?.trim();
    if (!idempotencyKey) {
      res.status(400).json({ detail: "Idempotency-Key header is required" });
      return;
    }

    let tenantId = "";
    let conversationHash = "";
    let filename = "material.bin";
    let contentType = "application/octet-stream";
    let dataBuf: Buffer | null = null;
    let sourceRef: string | null = null;
    let parentMaterialId: string | null = null;
    let ordinal: number | null = null;
    let provider: string | null = null;
    let model: string | null = null;
    let purpose: "inference_input" | "archive" = "inference_input";
    let metadata: Record<string, unknown> = {};

    if (req.file) {
      dataBuf = req.file.buffer;
      tenantId = String(req.body.tenant_id || "").trim();
      conversationHash = String(req.body.conversation_hash || "").trim();
      filename = String(req.body.filename || req.file.originalname || "material.bin");
      contentType = String(
        req.body.content_type || req.file.mimetype || "application/octet-stream"
      );
      sourceRef = req.body.source_ref ? String(req.body.source_ref) : null;
      parentMaterialId = req.body.parent_material_id
        ? String(req.body.parent_material_id)
        : null;
      ordinal = req.body.ordinal ? Number(req.body.ordinal) : null;
      provider = req.body.provider ? String(req.body.provider).toLowerCase() : null;
      model = req.body.model ? String(req.body.model) : null;
      purpose = req.body.purpose === "archive" ? "archive" : "inference_input";
      if (req.body.metadata) {
        try {
          metadata = JSON.parse(String(req.body.metadata)) as Record<string, unknown>;
        } catch {
          res.status(400).json({ detail: "metadata must be valid JSON" });
          return;
        }
      }
    } else {
      const parsed = MaterialCreateJSONSchema.safeParse(req.body);
      if (!parsed.success) {
        res.status(422).json({ detail: parsed.error.issues });
        return;
      }
      const p = parsed.data;
      tenantId = p.tenant_id;
      conversationHash = p.conversation_hash;
      filename = p.filename;
      contentType = p.content_type || "application/octet-stream";
      sourceRef = p.source_ref ?? null;
      parentMaterialId = p.parent_material_id ?? null;
      ordinal = p.ordinal ?? null;
      provider = p.provider ? p.provider.toLowerCase() : null;
      model = p.model ?? null;
      purpose = p.purpose;
      metadata = p.metadata;

      if (p.content_base64) {
        dataBuf = Buffer.from(p.content_base64, "base64");
      } else if (p.source_url) {
        try {
          const parsedUrl = new URL(p.source_url);
          if (
            parsedUrl.protocol !== "https:" &&
            !(config.materialAllowHttp && parsedUrl.protocol === "http:")
          ) {
            res.status(400).json({ detail: "Only HTTPS source_url is allowed" });
            return;
          }
          const resp = await fetch(parsedUrl.toString(), {
            signal: AbortSignal.timeout(config.materialIngressTimeoutSeconds * 1000),
          });
          if (!resp.ok) {
            res
              .status(502)
              .json({ detail: `Failed to fetch source_url: HTTP ${resp.status}` });
            return;
          }
          dataBuf = Buffer.from(await resp.arrayBuffer());
          if (!p.content_type && resp.headers.get("content-type")) {
            contentType = resp.headers.get("content-type")!.split(";")[0].trim();
          }
        } catch (err) {
          res.status(502).json({
            detail: `Failed to fetch source_url: ${err instanceof Error ? err.message : String(err)}`,
          });
          return;
        }
      }
    }

    if (!tenantId || !conversationHash) {
      res.status(400).json({ detail: "tenant_id and conversation_hash are required" });
      return;
    }
    if (!dataBuf) {
      res.status(400).json({ detail: "Material content is required" });
      return;
    }
    if (purpose === "inference_input" && (!provider || !model)) {
      res
        .status(400)
        .json({ detail: "provider and model are required for inference_input material" });
      return;
    }

    const idemKey = `${tenantId}:${idempotencyKey}`;
    const existingId = store.materialIdempotency.get(idemKey);
    if (existingId) {
      const existing = store.materials.get(existingId);
      if (existing) {
        res.status(201).json(formatMaterialResponse(existing));
        return;
      }
    }

    const storedObj = store.putObject(dataBuf, contentType);
    const route = provider && model ? resolveRoute(provider, model) : null;
    const matId = `mat_${crypto.randomUUID().replace(/-/g, "")}`;
    const row: MaterialRecord = {
      id: matId,
      tenant_id: tenantId,
      conversation_hash: conversationHash,
      idempotency_key: idempotencyKey,
      filename,
      content_type: contentType,
      size_bytes: dataBuf.byteLength,
      sha256: storedObj.sha256,
      provider,
      model,
      purpose,
      status: "ready",
      durability: "relay_backed",
      target_connection_id: route?.connection_id ?? null,
      object_id: storedObj.id,
      storage_id: config.defaultStorageId,
      source_ref: sourceRef,
      parent_material_id: parentMaterialId,
      ordinal,
      route_revision: route?.route_revision ?? null,
      metadata,
      created_at: new Date().toISOString(),
    };

    store.materials.set(matId, row);
    store.materialIdempotency.set(idemKey, matId);
    res.status(201).json(formatMaterialResponse(row));
  }
);

v2Router.get(
  "/materials/:materialId",
  requireOwnerHeaders,
  (req: Request, res: Response): void => {
    const owner = extractOwnerHeaders(req)!;
    const materialId = String(req.params.materialId || "");
    const row = store.materials.get(materialId);
    if (
      !row ||
      row.tenant_id !== owner.tenantId ||
      row.conversation_hash !== owner.conversationHash
    ) {
      res.status(404).json({ detail: "Material not found" });
      return;
    }
    res.json(formatMaterialResponse(row));
  }
);

v2Router.post("/sessions", async (req: Request, res: Response): Promise<void> => {
  const idempotencyKey = req.header("idempotency-key")?.trim();
  if (!idempotencyKey) {
    res.status(400).json({ detail: "Idempotency-Key header is required" });
    return;
  }
  const result = await createSessionCore(req.body, idempotencyKey);
  res.status(result.status).json(result.body);
});

v2Router.get(
  "/sessions/:sessionId",
  requireOwnerHeaders,
  (req: Request, res: Response): void => {
    const owner = extractOwnerHeaders(req)!;
    const sessionId = String(req.params.sessionId || "");
    const row = store.sessions.get(sessionId);
    if (
      !row ||
      row.tenant_id !== owner.tenantId ||
      row.conversation_hash !== owner.conversationHash
    ) {
      res.status(404).json({ detail: "Session not found" });
      return;
    }
    res.json(formatSessionResponse(row));
  }
);

v2Router.post(
  "/sessions/:sessionId/requests",
  requireOwnerHeaders,
  async (req: Request, res: Response): Promise<void> => {
    const idempotencyKey = req.header("idempotency-key")?.trim();
    if (!idempotencyKey) {
      res.status(400).json({ detail: "Idempotency-Key header is required" });
      return;
    }
    const owner = extractOwnerHeaders(req)!;
    const sessionId = String(req.params.sessionId || "");
    const result = await createSessionRequestCore(
      sessionId,
      req.body,
      idempotencyKey,
      owner
    );
    res.status(result.status).json(result.body);
  }
);

v2Router.get(
  "/sessions/:sessionId/requests/:requestId",
  requireOwnerHeaders,
  (req: Request, res: Response): void => {
    const owner = extractOwnerHeaders(req)!;
    const sessionId = String(req.params.sessionId || "");
    const requestId = String(req.params.requestId || "");
    const row = store.requests.get(requestId);
    if (
      !row ||
      row.session_id !== sessionId ||
      row.tenant_id !== owner.tenantId ||
      row.conversation_hash !== owner.conversationHash
    ) {
      res.status(404).json({ detail: "Request not found" });
      return;
    }
    res.json(formatRequestEnvelope(row));
  }
);

v2Router.get(
  "/sessions/:sessionId/requests/:requestId/result",
  requireOwnerHeaders,
  (req: Request, res: Response): void => {
    const owner = extractOwnerHeaders(req)!;
    const sessionId = String(req.params.sessionId || "");
    const requestId = String(req.params.requestId || "");
    const row = store.requests.get(requestId);
    if (
      !row ||
      row.session_id !== sessionId ||
      row.tenant_id !== owner.tenantId ||
      row.conversation_hash !== owner.conversationHash
    ) {
      res.status(404).json({ detail: "Request not found" });
      return;
    }
    const httpStatus =
      row.status === "queued" || row.status === "leased" || row.status === "running"
        ? 202
        : 200;
    res.status(httpStatus).json(formatRequestEnvelope(row));
  }
);

v2Router.get(
  "/sessions/:sessionId/requests/:requestId/error/raw",
  requireOwnerHeaders,
  (req: Request, res: Response): void => {
    const owner = extractOwnerHeaders(req)!;
    const sessionId = String(req.params.sessionId || "");
    const requestId = String(req.params.requestId || "");
    const row = store.requests.get(requestId);
    if (
      !row ||
      row.session_id !== sessionId ||
      row.tenant_id !== owner.tenantId ||
      row.conversation_hash !== owner.conversationHash
    ) {
      res.status(404).json({ detail: "Request not found" });
      return;
    }
    if (!row.raw_error_bytes) {
      res.status(404).json({ detail: "No archived raw error body is available" });
      return;
    }
    res
      .status(200)
      .contentType(row.raw_error_content_type || "application/octet-stream")
      .send(row.raw_error_bytes);
  }
);

v2Router.post(
  "/sessions/:sessionId/requests/:requestId/cancel",
  requireOwnerHeaders,
  (req: Request, res: Response): void => {
    const owner = extractOwnerHeaders(req)!;
    const sessionId = String(req.params.sessionId || "");
    const requestId = String(req.params.requestId || "");
    const row = store.requests.get(requestId);
    if (
      !row ||
      row.session_id !== sessionId ||
      row.tenant_id !== owner.tenantId ||
      row.conversation_hash !== owner.conversationHash
    ) {
      res.status(404).json({ detail: "Request not found" });
      return;
    }
    if (
      row.status !== "succeeded" &&
      row.status !== "failed" &&
      row.status !== "cancelled"
    ) {
      row.status = "cancelled";
      row.completed_at = new Date().toISOString();
      row.error = {
        source: "relay",
        code: "CANCELLED_BY_CLIENT",
        message: "Request cancelled by client",
      };
      const sess = store.sessions.get(sessionId);
      if (sess && sess.active_request_id === row.id) {
        sess.active_request_id = null;
      }
    }
    res.json(formatRequestEnvelope(row));
  }
);
