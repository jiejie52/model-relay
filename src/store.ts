import crypto from "node:crypto";
import type {
  ActualExecutionFact,
  AnchorPinRecord,
  CommittedContinuationAnchor,
  ContinuationExecutionBinding,
  HistoryTurn,
  JobRecord,
  MaterialOccurrenceRef,
  MaterialRecord,
  ProviderMaterialBindingRecord,
  RequestRecord,
  SessionRecord,
} from "./types.js";

export interface StoredObject {
  id: string;
  data: Buffer;
  contentType: string;
  sha256: string;
}

class InMemoryRelayStore {
  readonly materials = new Map<string, MaterialRecord>();
  readonly materialIdempotency = new Map<string, string>();
  readonly materialBindings = new Map<string, ProviderMaterialBindingRecord>();

  readonly sessions = new Map<string, SessionRecord>();
  readonly sessionIdempotency = new Map<string, string>();

  readonly requests = new Map<string, RequestRecord>();
  readonly requestIdempotency = new Map<string, string>();

  readonly jobs = new Map<string, JobRecord>();
  readonly jobIdempotency = new Map<string, string>();

  readonly objects = new Map<string, StoredObject>();
  readonly jsonArtifacts = new Map<string, Record<string, unknown>>();
  readonly histories = new Map<string, HistoryTurn[]>();

  // Per-session Anchor Ledger (ordered DAG/Tree nodes, never a single overwrite slot)
  readonly continuationAnchors = new Map<string, CommittedContinuationAnchor[]>();
  readonly anchorPins = new Map<string, AnchorPinRecord[]>();
  readonly executionFacts = new Map<string, ActualExecutionFact>();

  putObject(data: Buffer, contentType: string): StoredObject {
    const id = crypto.randomUUID();
    const sha256 = crypto.createHash("sha256").update(data).digest("hex");
    const obj: StoredObject = { id, data, contentType, sha256 };
    this.objects.set(id, obj);
    return obj;
  }

  getObject(id: string): StoredObject | undefined {
    return this.objects.get(id);
  }

  putJsonArtifact(payload: Record<string, unknown>): string {
    const id = crypto.randomUUID();
    this.jsonArtifacts.set(id, payload);
    return id;
  }

  getJsonArtifact(id: string): Record<string, unknown> | undefined {
    return this.jsonArtifacts.get(id);
  }

  getHistory(sessionId: string): HistoryTurn[] {
    return this.histories.get(sessionId) ?? [];
  }

  appendHistory(sessionId: string, turns: HistoryTurn[]): number {
    const existing = this.histories.get(sessionId) ?? [];
    const updated = [...existing, ...turns];
    this.histories.set(sessionId, updated);
    // Keep one history_version per turn pair (user + model)
    return Math.ceil(updated.length / 2);
  }

  getOrCreateMaterialBinding(
    material: MaterialRecord,
    accountScopeId: string,
    connectionId: string,
    representation = "inline_or_text"
  ): ProviderMaterialBindingRecord {
    const key = `${material.id}:${accountScopeId}:${connectionId}`;
    const existing = this.materialBindings.get(key);
    if (existing && existing.status === "ready") {
      return existing;
    }
    const nextGen = existing ? existing.binding_generation + 1 : 1;
    const expiresAt = new Date(Date.now() + 48 * 3600 * 1000).toISOString();
    const record: ProviderMaterialBindingRecord = {
      material_id: material.id,
      account_scope_id: accountScopeId,
      connection_id: connectionId,
      binding_generation: nextGen,
      provider_binding_id: null,
      external_uri: null,
      representation,
      status: "ready",
      expires_at: expiresAt,
    };
    this.materialBindings.set(key, record);
    return record;
  }

  listSessionAnchors(sessionId: string): CommittedContinuationAnchor[] {
    const list = this.continuationAnchors.get(sessionId) ?? [];
    return [...list].sort((a, b) => {
      if (b.covered_turn_count !== a.covered_turn_count) {
        return b.covered_turn_count - a.covered_turn_count;
      }
      return b.history_version - a.history_version;
    });
  }

  findAnchorById(sessionId: string, anchorId: string): CommittedContinuationAnchor | undefined {
    const list = this.continuationAnchors.get(sessionId) ?? [];
    return list.find((a) => a.id === anchorId);
  }

  hasUnreconciledIndeterminateRequest(sessionId: string): boolean {
    for (const req of this.requests.values()) {
      if (
        req.session_id === sessionId &&
        (req.status === "indeterminate" || req.provider_dispatch_state === "indeterminate")
      ) {
        return true;
      }
    }
    return false;
  }

  /**
   * Single atomic seal transition (not_sent -> dispatch_started) matching seal_continuation_context_v91.
   * Re-verifies execution fence, material bindings, and parent anchor validity before pinning and sealing.
   */
  sealContinuationContext(params: {
    session: SessionRecord;
    request: RequestRecord;
    binding: ContinuationExecutionBinding;
    preparedPayloadHash: string;
    anchorSafetyMarginSeconds: number;
  }): { ok: true } | { ok: false; code: string; message: string } {
    if (params.request.provider_dispatch_state !== "not_sent") {
      return {
        ok: false,
        code: "DISPATCH_FENCE_ALREADY_CROSSED",
        message: `Cannot seal request in state ${params.request.provider_dispatch_state}`,
      };
    }

    if (params.binding.selected_anchor_id) {
      const anchor = this.findAnchorById(
        params.session.id,
        params.binding.selected_anchor_id
      );
      const nowMs = Date.now();
      const marginMs = params.anchorSafetyMarginSeconds * 1000;
      if (
        !anchor ||
        anchor.status !== "active" ||
        new Date(anchor.expires_at).getTime() <= nowMs + marginMs
      ) {
        return {
          ok: false,
          code: "CONTINUATION_PARENT_NO_LONGER_USABLE",
          message: "Selected parent anchor expired or invalidated prior to dispatch seal",
        };
      }

      // Re-verify all covered material bindings haven't rotated or expired since bind step
      for (const ref of anchor.covered_material_bindings) {
        const key = `${ref.material_id}:${params.session.route.account_scope_id}:${params.session.route.connection_id}`;
        const live = this.materialBindings.get(key);
        if (
          !live ||
          live.status !== "ready" ||
          live.binding_generation !== ref.binding_generation ||
          new Date(live.expires_at).getTime() <= nowMs + marginMs
        ) {
          return {
            ok: false,
            code: "CONTINUATION_PARENT_NO_LONGER_USABLE",
            message: `Material binding ${ref.material_id} rotated or expired prior to dispatch seal`,
          };
        }
      }

      const pins = this.anchorPins.get(params.request.id) ?? [];
      pins.push({
        request_id: params.request.id,
        anchor_id: anchor.id,
        status: "held",
        created_at: new Date().toISOString(),
        released_at: null,
      });
      this.anchorPins.set(params.request.id, pins);
    }

    params.request.sealed_continuation_binding = params.binding;
    params.request.prepared_payload_hash = params.preparedPayloadHash;
    params.request.provider_dispatch_state = "dispatch_started";
    return { ok: true };
  }

  /**
   * Atomic commit matching complete_continuation_context_v91:
   * verifies fence & result_stored, advances history, appends material ledger,
   * publishes new anchor (idempotent on source_request_id), and releases held pins.
   */
  completeContinuationContext(params: {
    session: SessionRecord;
    request: RequestRecord;
    newTurns: HistoryTurn[];
    materialOccurrences: MaterialOccurrenceRef[];
    anchorCandidate: CommittedContinuationAnchor | null;
    resultEnvelope: Record<string, unknown>;
    cacheSummary: Record<string, unknown>;
    continuationSummary: Record<string, unknown> | null;
  }): number {
    if (
      params.request.provider_dispatch_state !== "result_stored" &&
      params.request.provider_dispatch_state !== "committed"
    ) {
      throw new Error(
        `Invalid dispatch state for commit: ${params.request.provider_dispatch_state}`
      );
    }

    // Idempotent check if already committed via prior result_stored recovery
    if (params.request.provider_dispatch_state === "committed") {
      return params.session.history_version;
    }

    if (params.session.history_version !== params.request.expected_history_version) {
      throw new Error("CONTINUATION_OPTIMISTIC_HISTORY_VERSION_CONFLICT");
    }

    let nextHistoryVersion = params.session.history_version;
    if (
      params.session.context_policy === "conversation" &&
      params.session.history_mode === "cumulative" &&
      params.newTurns.length > 0
    ) {
      nextHistoryVersion = this.appendHistory(params.session.id, params.newTurns);
      params.session.history_version = nextHistoryVersion;
    }

    if (params.materialOccurrences.length > 0) {
      params.session.committed_material_ledger.push(...params.materialOccurrences);
    }

    if (params.anchorCandidate) {
      const list = this.continuationAnchors.get(params.session.id) ?? [];
      const existingBySource = list.find(
        (a) => a.source_request_id === params.anchorCandidate!.source_request_id
      );
      if (!existingBySource) {
        if (params.anchorCandidate.anchor_class === "linear_ephemeral_cursor") {
          const nowIso = new Date().toISOString();
          for (const item of list) {
            if (item.status === "active") {
              item.status = "superseded";
              item.updated_at = nowIso;
            }
          }
        }
        list.push(params.anchorCandidate);
        this.continuationAnchors.set(params.session.id, list);
        params.request.produced_anchor_id = params.anchorCandidate.id;
      } else {
        params.request.produced_anchor_id = existingBySource.id;
      }
    }

    // Release anchor pins
    const pins = this.anchorPins.get(params.request.id) ?? [];
    const releasedAt = new Date().toISOString();
    for (const pin of pins) {
      if (pin.status !== "released") {
        pin.status = "released";
        pin.released_at = releasedAt;
      }
    }

    params.request.provider_dispatch_state = "committed";
    params.request.status = "succeeded";
    params.request.completed_at = releasedAt;
    params.request.history_version = nextHistoryVersion;
    params.request.cache = params.cacheSummary;
    params.request.continuation = params.continuationSummary;
    params.request.result = params.resultEnvelope;
    params.session.active_request_id = null;
    return nextHistoryVersion;
  }

  /**
   * Release pins & optionally invalidate rejected parent anchor atomically on failure.
   */
  releaseContinuationAfterRequest(params: {
    session: SessionRecord;
    request: RequestRecord;
    invalidateAnchorId: string | null;
    invalidationReason: string | null;
    markIndeterminate?: boolean;
  }): void {
    const nowIso = new Date().toISOString();
    if (params.markIndeterminate) {
      params.request.provider_dispatch_state = "indeterminate";
      params.request.status = "indeterminate";
      // Keep active_request_id and held anchor pins locked on indeterminate
      return;
    }

    if (params.invalidateAnchorId) {
      const target = this.findAnchorById(params.session.id, params.invalidateAnchorId);
      if (target && target.status === "active") {
        target.status = "invalidated";
        target.invalidated_by_request_id = params.request.id;
        target.invalidation_reason = params.invalidationReason;
        target.updated_at = nowIso;
      }
    }

    const pins = this.anchorPins.get(params.request.id) ?? [];
    for (const pin of pins) {
      if (pin.status !== "released") {
        pin.status = "released";
        pin.released_at = nowIso;
      }
    }

    params.request.provider_dispatch_state = "failed";
    params.request.status = "failed";
    params.request.completed_at = nowIso;
    params.session.active_request_id = null;
  }
}

export const store = new InMemoryRelayStore();

export function stableHash(value: unknown): string {
  const canonical = stableStringify(value);
  return crypto.createHash("sha256").update(canonical, "utf8").digest("hex");
}

function stableStringify(value: unknown): string {
  if (value === null || typeof value !== "object") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return "[" + value.map((item) => stableStringify(item)).join(",") + "]";
  }
  const obj = value as Record<string, unknown>;
  const keys = Object.keys(obj).sort();
  return (
    "{" +
    keys
      .filter((k) => obj[k] !== undefined)
      .map((k) => JSON.stringify(k) + ":" + stableStringify(obj[k]))
      .join(",") +
    "}"
  );
}
