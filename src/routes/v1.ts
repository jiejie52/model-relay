import crypto from "node:crypto";
import { Router, type Request, type Response } from "express";
import { config } from "../config.js";
import { resolveRoute } from "../controlPlane.js";
import { extractOwnerHeaders, requireOwnerHeaders, requireRelayAuth } from "../middleware/auth.js";
import { executeProviderCall } from "../providers.js";
import { stableHash, store } from "../store.js";
import { FUSION_STAGES, JobSubmitRequestSchema, type JobRecord } from "../types.js";
import { createSessionCore, createSessionRequestCore, formatRequestEnvelope } from "./v2.js";

export const v1Router = Router();
v1Router.use(requireRelayAuth);

v1Router.post("/dify/relay", async (req: Request, res: Response): Promise<void> => {
  const envelope = req.body as Record<string, unknown>;
  if (!envelope || typeof envelope !== "object" || Array.isArray(envelope)) {
    res.status(400).json({ detail: "request body must be a JSON object" });
    return;
  }

  const contract = String(envelope.execution_contract || "v1");
  const operation = String(envelope.operation || "").trim().toLowerCase();
  const idempotencyKey = req.header("idempotency-key")?.trim();
  const owner = extractOwnerHeaders(req);

  if (contract === "v2") {
    if (!owner) {
      res.status(400).json({ detail: "Owner headers are required" });
      return;
    }
    let sessionId = envelope.session_id ? String(envelope.session_id) : "";

    if (operation === "execute") {
      if (!idempotencyKey) {
        res.status(400).json({ detail: "V2 execute requires a stable Idempotency-Key" });
        return;
      }
      if (!sessionId) {
        const sessionKey = envelope.session_key;
        if (typeof sessionKey !== "string" || !sessionKey || sessionKey.length > 490) {
          res
            .status(400)
            .json({ detail: "A stable session_key is required when creating a Session" });
          return;
        }
        const sessionObj = envelope.session;
        if (!sessionObj || typeof sessionObj !== "object" || Array.isArray(sessionObj)) {
          res.status(422).json({ detail: "session must be an object" });
          return;
        }
        const createdSession = await createSessionCore(
          {
            ...(sessionObj as Record<string, unknown>),
            tenant_id: owner.tenantId,
            conversation_hash: owner.conversationHash,
          },
          `dify-v2:${sessionKey}`
        );
        if (createdSession.status >= 300) {
          res.status(createdSession.status).json(createdSession.body);
          return;
        }
        sessionId = String(createdSession.body.session_id);
      }

      const reqResult = await createSessionRequestCore(
        sessionId,
        envelope.request,
        idempotencyKey,
        owner
      );
      const data = reqResult.body;
      res.status(reqResult.status).json({
        execution_contract: "v2",
        operation,
        ok: reqResult.status < 300,
        session_id: data.session_id ?? sessionId,
        request_id: data.request_id ?? envelope.request_id ?? null,
        job_id: data.job_id ?? null,
        status: data.status ?? null,
        result: data.result ?? null,
        error: data.error ?? null,
        payload: data,
      });
      return;
    }

    if (operation === "status" || operation === "result") {
      const requestId = envelope.request_id ? String(envelope.request_id) : "";
      if (!sessionId || !requestId) {
        res.status(422).json({ detail: "V2 queries require session_id and request_id" });
        return;
      }
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
      const data = formatRequestEnvelope(row);
      const httpStatus =
        operation === "result" &&
        (row.status === "queued" || row.status === "leased" || row.status === "running")
          ? 202
          : 200;
      res.status(httpStatus).json({
        execution_contract: "v2",
        operation,
        ok: true,
        session_id: data.session_id,
        request_id: data.request_id,
        job_id: data.job_id,
        status: data.status,
        result: data.result,
        error: data.error,
        payload: data,
      });
      return;
    }

    res.status(400).json({ detail: "operation must be execute, status, or result" });
    return;
  }

  if (contract !== "v1") {
    res.status(400).json({ detail: "Unknown execution_contract" });
    return;
  }

  if (operation === "execute") {
    const jobReq = envelope.request as Record<string, unknown> | undefined;
    if (!jobReq || typeof jobReq !== "object") {
      res.status(400).json({ detail: "execute.request must be an object" });
      return;
    }
    const effectiveKey =
      idempotencyKey || `dify-gateway-${stableHash(jobReq)}`;
    const jobResult = await submitV1JobCore(jobReq, effectiveKey);
    const state = String(jobResult.body.status || "failed");
    res.status(jobResult.status).json({
      ok: jobResult.status >= 200 && jobResult.status < 300 && Boolean(jobResult.body.job_id),
      operation: "execute",
      state,
      status: state,
      job_id: String(jobResult.body.job_id || ""),
      relay_session_id: String(jobResult.body.relay_session_id || ""),
      upstream_status: jobResult.status,
      error: jobResult.body.detail ?? null,
    });
    return;
  }

  if (operation === "status" || operation === "result") {
    const jobId = String(envelope.job_id || "").trim();
    if (!jobId) {
      res.status(400).json({ detail: `${operation}.job_id is required` });
      return;
    }
    const job = store.jobs.get(jobId);
    if (!job) {
      res.status(404).json({ detail: "Job not found" });
      return;
    }
    if (operation === "status") {
      res.status(200).json({
        ok: true,
        operation: "status",
        state: job.status,
        status: job.status,
        job_id: job.id,
        relay_session_id: job.relay_session_id || "",
        heartbeat_at: "",
        upstream_status: 200,
        error: job.error_message,
      });
      return;
    }

    const httpStatus = job.status === "queued" || job.status === "running" ? 202 : 200;
    const resultObj =
      job.status === "succeeded"
        ? job.compact_result || { job_id: job.id, status: "succeeded", text: "" }
        : job.status === "failed" || job.status === "cancelled"
          ? {
              job_id: job.id,
              status: job.status,
              error: { source: "relay", code: job.error_code, message: job.error_message },
            }
          : { job_id: job.id, status: job.status, stage: job.stage, poll_after_seconds: 10 };

    res.status(httpStatus).json({
      ok: httpStatus === 200,
      operation: "result",
      state: job.status,
      status: job.status,
      job_id: job.id,
      upstream_status: httpStatus,
      error: (resultObj as Record<string, unknown>).error ?? null,
      result: resultObj,
      ...resultObj,
    });
    return;
  }

  res.status(400).json({ detail: "operation must be execute, status, or result" });
});

async function submitV1JobCore(
  rawBody: unknown,
  idempotencyKey: string
): Promise<{ status: number; body: Record<string, unknown> }> {
  const parsed = JobSubmitRequestSchema.safeParse(rawBody);
  if (!parsed.success) {
    return { status: 422, body: { detail: parsed.error.issues } };
  }
  const body = parsed.data;
  if (FUSION_STAGES.has(body.stage)) {
    return { status: 501, body: { error: "Not yet migrated" } };
  }
  if (!body.current_query?.trim()) {
    return { status: 422, body: { detail: "normal jobs require current_query" } };
  }

  const idemKey = `${body.tenant_id}:${idempotencyKey}`;
  const existingId = store.jobIdempotency.get(idemKey);
  if (existingId) {
    const existing = store.jobs.get(existingId);
    if (existing) {
      return {
        status: 202,
        body: {
          job_id: existing.id,
          relay_session_id: existing.relay_session_id,
          status: existing.status,
          poll_after_seconds: 5,
          submitted_at: existing.created_at,
          expires_at: existing.expires_at,
        },
      };
    }
  }

  const now = new Date();
  const jobId = crypto.randomUUID();
  const job: JobRecord = {
    id: jobId,
    tenant_id: body.tenant_id,
    conversation_hash: body.conversation_hash,
    relay_session_id: body.relay_session_id ?? null,
    stage: body.stage,
    provider: body.provider,
    model: body.model,
    think_level: body.think_level,
    status: "queued",
    idempotency_key: idempotencyKey,
    compact_result: null,
    error_code: null,
    error_message: null,
    created_at: now.toISOString(),
    started_at: null,
    completed_at: null,
    expires_at: new Date(now.getTime() + config.jobTtlSeconds * 1000).toISOString(),
  };

  store.jobs.set(jobId, job);
  store.jobIdempotency.set(idemKey, jobId);

  setImmediate(async () => {
    job.status = "running";
    job.started_at = new Date().toISOString();
    const route = resolveRoute(body.provider, body.model);
    const res = await executeProviderCall({
      route,
      input: body.current_query,
      instructions: body.instructions ?? null,
      history: [],
      materials: [],
      thinkLevel: body.think_level,
      options: {},
      structuredOutput: body.structured_output,
      previousInteractionId: null,
      cacheMode: "auto",
      cacheMechanism: "auto",
    });
    job.completed_at = new Date().toISOString();
    if (res.ok) {
      job.status = "succeeded";
      job.compact_result = {
        job_id: job.id,
        status: "succeeded",
        text: res.text,
        usage: res.usage,
      };
    } else {
      job.status = "failed";
      job.error_code = res.error.code;
      job.error_message = res.error.message;
    }
  });

  return {
    status: 202,
    body: {
      job_id: job.id,
      relay_session_id: job.relay_session_id,
      status: job.status,
      poll_after_seconds: 5,
      submitted_at: job.created_at,
      expires_at: job.expires_at,
    },
  };
}

v1Router.post("/jobs", async (req: Request, res: Response): Promise<void> => {
  const idempotencyKey = req.header("idempotency-key")?.trim();
  if (!idempotencyKey) {
    res.status(400).json({ detail: "Idempotency-Key header is required" });
    return;
  }
  const result = await submitV1JobCore(req.body, idempotencyKey);
  res.status(result.status).json(result.body);
});

v1Router.get("/jobs/:jobId", requireOwnerHeaders, (req: Request, res: Response): void => {
  const owner = extractOwnerHeaders(req)!;
  const jobId = String(req.params.jobId || "");
  const job = store.jobs.get(jobId);
  if (
    !job ||
    job.tenant_id !== owner.tenantId ||
    job.conversation_hash !== owner.conversationHash
  ) {
    res.status(404).json({ detail: "Job not found" });
    return;
  }
  res.json({
    job_id: job.id,
    relay_session_id: job.relay_session_id,
    status: job.status,
    stage: job.stage,
    provider: job.provider,
    model: job.model,
    heartbeat_at: null,
    started_at: job.started_at,
    completed_at: job.completed_at,
    poll_after_seconds: 10,
    retryable: false,
    error_code: job.error_code,
    error_message: job.error_message,
  });
});

v1Router.get("/jobs/:jobId/result", requireOwnerHeaders, (req: Request, res: Response): void => {
  const owner = extractOwnerHeaders(req)!;
  const jobId = String(req.params.jobId || "");
  const job = store.jobs.get(jobId);
  if (
    !job ||
    job.tenant_id !== owner.tenantId ||
    job.conversation_hash !== owner.conversationHash
  ) {
    res.status(404).json({ detail: "Job not found" });
    return;
  }
  if (job.status === "succeeded") {
    res.status(200).json(job.compact_result || { job_id: job.id, status: "succeeded" });
    return;
  }
  if (job.status === "failed" || job.status === "cancelled" || job.status === "expired") {
    res.status(200).json({
      job_id: job.id,
      status: job.status,
      error: { source: "relay", code: job.error_code, message: job.error_message },
    });
    return;
  }
  res.status(202).json({
    job_id: job.id,
    status: job.status,
    stage: job.stage,
    poll_after_seconds: 10,
  });
});

v1Router.post("/jobs/:jobId/cancel", requireOwnerHeaders, (req: Request, res: Response): void => {
  const owner = extractOwnerHeaders(req)!;
  const jobId = String(req.params.jobId || "");
  const job = store.jobs.get(jobId);
  if (
    !job ||
    job.tenant_id !== owner.tenantId ||
    job.conversation_hash !== owner.conversationHash
  ) {
    res.status(404).json({ detail: "Job not found" });
    return;
  }
  if (job.status !== "succeeded" && job.status !== "failed" && job.status !== "cancelled") {
    job.status = "cancelled";
    job.completed_at = new Date().toISOString();
    job.error_code = "CANCELLED_BY_CLIENT";
    job.error_message = "Job cancelled by client";
  }
  res.json({ job_id: job.id, status: job.status });
});
