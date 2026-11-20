import crypto from "node:crypto";
import { GoogleGenAI, type Content, type Part } from "@google/genai";
import { config } from "./config.js";
import { store } from "./store.js";
import type {
  HistoryTurn,
  MaterialRecord,
  NativeStepBlock,
  RouteBinding,
} from "./types.js";

export interface ProviderExecutionInput {
  route: RouteBinding;
  input: unknown;
  instructions: string | null;
  tools?: unknown[];
  history: HistoryTurn[];
  deltaHistory?: HistoryTurn[];
  omittedHistory?: HistoryTurn[];
  materials: MaterialRecord[];
  thinkLevel: string | null;
  options: Record<string, unknown>;
  structuredOutput: Record<string, unknown>;
  previousInteractionId: string | null;
  storeUpstreamState?: boolean;
  requiresThoughtSignatureOnReplay?: boolean;
  cacheMode: string;
  cacheMechanism: string;
}

export interface ProviderExecutionSuccess {
  ok: true;
  text: string;
  rawText: string;
  model: string;
  interactionId: string | null;
  nativeSteps: NativeStepBlock[];
  upstreamStateStored: boolean;
  previousAnchorRef: string | null;
  usage: {
    prompt_tokens: number | null;
    completion_tokens: number | null;
    total_tokens: number | null;
    cached_tokens: number | null;
    thinking_tokens: number | null;
    omitted_history_estimated_tokens: number;
  };
  continuationUsed: boolean;
  fallbackOccurred: boolean;
  fallbackReason: string | null;
}

export interface ProviderExecutionFailure {
  ok: false;
  httpStatus: number;
  error: {
    source: string;
    code: string;
    message: string;
    upstream_http_status?: number;
    body_text?: string;
    body_base64?: string;
    body_sha256?: string;
  };
  rawBytes?: Buffer;
  rawContentType?: string;
}

export type ProviderExecutionResult = ProviderExecutionSuccess | ProviderExecutionFailure;

export function extractInputText(input: unknown): string {
  if (typeof input === "string") return input;
  if (Array.isArray(input)) {
    return input
      .map((item) => {
        if (typeof item === "string") return item;
        if (item && typeof item === "object") {
          const obj = item as Record<string, unknown>;
          if (typeof obj.text === "string") return obj.text;
          if (typeof obj.content === "string") return obj.content;
        }
        return JSON.stringify(item);
      })
      .join("\n");
  }
  if (input && typeof input === "object") {
    const obj = input as Record<string, unknown>;
    if (typeof obj.text === "string") return obj.text;
    if (typeof obj.content === "string") return obj.content;
    if (typeof obj.query === "string") return obj.query;
  }
  return JSON.stringify(input ?? "");
}

function estimateOmittedHistoryTokens(omittedHistory: HistoryTurn[] | undefined): number {
  if (!omittedHistory || omittedHistory.length === 0) return 0;
  let totalChars = 0;
  for (const turn of omittedHistory) {
    totalChars += (turn.text || "").length;
    if (turn.native_steps) {
      totalChars += JSON.stringify(turn.native_steps).length;
    }
  }
  return Math.ceil(totalChars / 4);
}

function buildStructuredOutputConfig(structuredOutput: Record<string, unknown>): {
  mode: "none" | "json_object" | "json_schema";
  schema?: Record<string, unknown>;
  name?: string;
} {
  const mode = String(structuredOutput.mode || "none").toLowerCase();
  if (
    mode === "json_schema" &&
    structuredOutput.schema &&
    typeof structuredOutput.schema === "object"
  ) {
    return {
      mode: "json_schema",
      schema: structuredOutput.schema as Record<string, unknown>,
      name:
        typeof structuredOutput.name === "string"
          ? structuredOutput.name
          : "response_schema",
    };
  }
  if (mode === "json_object" || mode === "object") {
    return { mode: "json_object" };
  }
  return { mode: "none" };
}

function materialToGeminiPart(material: MaterialRecord): Part | null {
  const obj = store.getObject(material.object_id);
  if (!obj) return null;
  if (
    material.content_type.startsWith("text/") ||
    material.content_type === "application/json"
  ) {
    return {
      text: `[Material: ${material.filename}]\n${obj.data.toString("utf8")}`,
    };
  }
  return {
    inlineData: {
      mimeType: material.content_type,
      data: obj.data.toString("base64"),
    },
  };
}

function materialToInteractionPart(material: MaterialRecord): Record<string, unknown> | null {
  const obj = store.getObject(material.object_id);
  if (!obj) return null;
  const rawMime = material.content_type || "application/octet-stream";
  const mime = rawMime.split(";")[0].trim() || "application/octet-stream";
  if (mime.startsWith("text/") || mime === "application/json") {
    return {
      type: "text",
      text: `[Material: ${material.filename}]\n${obj.data.toString("utf8")}`,
    };
  }
  const blockType = mime.startsWith("image/")
    ? "image"
    : mime.startsWith("audio/")
      ? "audio"
      : mime.startsWith("video/")
        ? "video"
        : "document";
  return {
    type: blockType,
    data: obj.data.toString("base64"),
    mime_type: mime,
  };
}

function extractInteractionsTextAndSteps(data: Record<string, unknown>): {
  text: string;
  steps: NativeStepBlock[];
} {
  const steps: NativeStepBlock[] = [];
  if (Array.isArray(data.steps)) {
    for (const s of data.steps) {
      if (s && typeof s === "object" && !Array.isArray(s)) {
        steps.push(s as NativeStepBlock);
      }
    }
  }

  // Prefer extracting text from outputs, falling back to steps[].model_output.content[] (UPGRADE 10.1.1)
  const parts: string[] = [];
  const outputs = Array.isArray(data.outputs) ? data.outputs : [];
  for (const item of outputs) {
    if (!item || typeof item !== "object") continue;
    const obj = item as Record<string, unknown>;
    if (obj.type === "text" && typeof obj.text === "string") {
      parts.push(obj.text);
    } else if (typeof obj.content === "string") {
      parts.push(obj.content);
    }
  }

  if (parts.length === 0 && steps.length > 0) {
    for (const step of steps) {
      if (step.type === "model_output") {
        const contentArr = Array.isArray(step.content)
          ? step.content
          : step.model_output &&
              typeof step.model_output === "object" &&
              Array.isArray((step.model_output as Record<string, unknown>).content)
            ? ((step.model_output as Record<string, unknown>).content as Array<
                Record<string, unknown>
              >)
            : [];
        for (const c of contentArr) {
          if (c && c.type === "text" && typeof c.text === "string") {
            parts.push(c.text);
          }
        }
      }
    }
  }

  if (parts.length > 0) {
    return { text: parts.join(""), steps };
  }
  if (typeof data.text === "string") {
    return { text: data.text, steps };
  }
  return { text: "", steps };
}

function projectHistoryTurnForInteractions(
  turn: HistoryTurn,
  requiresThoughtSignature: boolean
):
  | { ok: true; wireItems: Array<Record<string, unknown>> }
  | { ok: false; code: string; message: string } {
  if (turn.role === "user") {
    const content: Record<string, unknown>[] = [];
    for (const matId of turn.material_ids ?? []) {
      const mat = store.materials.get(matId);
      if (mat) {
        const part = materialToInteractionPart(mat);
        if (part) content.push(part);
      }
    }
    content.push({ type: "text", text: turn.text });
    return {
      ok: true,
      wireItems: [{ role: "user", content }],
    };
  }

  // Model turn: faithfully replay native steps (thought + signature, tool calls, model_output)
  if (turn.native_steps && turn.native_steps.length > 0) {
    const modelContent: Record<string, unknown>[] = [];
    for (const step of turn.native_steps) {
      if (step.type === "thought") {
        if (
          requiresThoughtSignature &&
          (typeof step.signature !== "string" || step.signature.trim().length === 0)
        ) {
          return {
            ok: false,
            code: "MISSING_THOUGHT_SIGNATURE_ON_REPLAY",
            message:
              "Historical thought step is missing required encrypted signature for replay",
          };
        }
        modelContent.push({
          type: "thought",
          ...(step.signature ? { signature: step.signature } : {}),
          ...(step.summary ? { summary: step.summary } : {}),
        });
      } else if (step.type === "model_output") {
        const stepContent = Array.isArray(step.content) ? step.content : [];
        for (const c of stepContent) {
          modelContent.push(c);
        }
      } else if (
        step.type === "google_search_call" ||
        step.type === "code_execution_call" ||
        step.type === "url_context_call" ||
        step.type === "file_search_call" ||
        step.type === "function_call"
      ) {
        modelContent.push({ ...step });
      }
    }

    if (modelContent.length > 0) {
      return {
        ok: true,
        wireItems: [{ role: "model", content: modelContent }],
      };
    }
  }

  return {
    ok: true,
    wireItems: [{ role: "model", content: [{ type: "text", text: turn.text }] }],
  };
}

async function executeGeminiInteractions(
  params: ProviderExecutionInput
): Promise<ProviderExecutionResult> {
  const apiKey = config.geminiApiKey;
  if (!apiKey) {
    return {
      ok: false,
      httpStatus: 503,
      error: {
        source: "relay",
        code: "PROVIDER_CREDENTIALS_NOT_CONFIGURED",
        message:
          "GEMINI_API_KEY or GOOGLE_GEMINI_API_KEY is not configured on the server",
      },
    };
  }

  const inputText = extractInputText(params.input);
  const structured = buildStructuredOutputConfig(params.structuredOutput);
  const inputItems: Record<string, unknown>[] = [];

  for (const mat of params.materials) {
    const part = materialToInteractionPart(mat);
    if (part) inputItems.push(part);
  }
  inputItems.push({ type: "text", text: inputText });

  const turnsToReplay =
    params.deltaHistory !== undefined
      ? params.deltaHistory
      : params.previousInteractionId
        ? []
        : params.history;

  const requiresThoughtSignature = params.requiresThoughtSignatureOnReplay ?? true;
  let wireInput: unknown = inputItems;

  if (turnsToReplay.length > 0) {
    const sequence: Record<string, unknown>[] = [];
    for (const turn of turnsToReplay) {
      const projected = projectHistoryTurnForInteractions(turn, requiresThoughtSignature);
      if (!projected.ok) {
        return {
          ok: false,
          httpStatus: 422,
          error: {
            source: "relay",
            code: projected.code,
            message: projected.message,
          },
        };
      }
      sequence.push(...projected.wireItems);
    }
    sequence.push({ role: "user", content: inputItems });
    wireInput = sequence;
  }

  const generationConfig: Record<string, unknown> = {};
  if (typeof params.options.temperature === "number") {
    generationConfig.temperature = params.options.temperature;
  }
  if (typeof params.options.top_p === "number") {
    generationConfig.top_p = params.options.top_p;
  }
  if (typeof params.options.max_output_tokens === "number") {
    generationConfig.max_output_tokens = params.options.max_output_tokens;
  }
  const thinkLower = (params.thinkLevel || "").toLowerCase();
  if (
    thinkLower &&
    thinkLower !== "auto" &&
    thinkLower !== "off" &&
    thinkLower !== "none"
  ) {
    // UPGRADE 10.1.2: thinking_level must be lowercase
    generationConfig.thinking_level = thinkLower;
  }

  const body: Record<string, unknown> = {
    model: params.route.upstream_model,
    input: wireInput,
    store: params.storeUpstreamState ?? true,
  };

  if (params.previousInteractionId) {
    body.previous_interaction_id = params.previousInteractionId;
  }
  if (params.instructions) {
    body.system_instruction = params.instructions;
  }
  if (params.tools && params.tools.length > 0) {
    body.tools = params.tools;
  }
  if (Object.keys(generationConfig).length > 0) {
    body.generation_config = generationConfig;
  }
  // UPGRADE 10.1.4: json_object does NOT send response_format to Interactions;
  // only explicit json_schema projects response_format.
  if (structured.mode === "json_schema" && structured.schema) {
    body.response_format = {
      type: "text",
      mime_type: "application/json",
      schema: structured.schema,
    };
  }

  const url = `${config.aihubmixGeminiBaseUrl}/v1beta/interactions`;
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-goog-api-key": apiKey,
    },
    body: JSON.stringify(body),
  });

  const rawBuf = Buffer.from(await resp.arrayBuffer());
  const rawText = rawBuf.toString("utf8");
  const sha = crypto.createHash("sha256").update(rawBuf).digest("hex");

  if (!resp.ok) {
    return {
      ok: false,
      httpStatus: 502,
      error: {
        source: "upstream",
        code: "UPSTREAM_HTTP_ERROR",
        message: rawText,
        upstream_http_status: resp.status,
        body_text: rawText,
        body_base64: rawBuf.toString("base64"),
        body_sha256: sha,
      },
      rawBytes: rawBuf,
      rawContentType: resp.headers.get("content-type") || "application/json",
    };
  }

  let data: Record<string, unknown>;
  try {
    data = JSON.parse(rawText) as Record<string, unknown>;
  } catch {
    return {
      ok: false,
      httpStatus: 502,
      error: {
        source: "upstream",
        code: "UPSTREAM_INVALID_JSON",
        message: "Gemini interactions response was not valid JSON",
        body_text: rawText,
      },
    };
  }

  const { text, steps } = extractInteractionsTextAndSteps(data);
  const usageMeta = (data.usage || data.usageMetadata || {}) as Record<string, unknown>;
  const interactionId = typeof data.id === "string" ? data.id : null;

  return {
    ok: true,
    text,
    rawText: text,
    model: String(data.model || params.route.upstream_model),
    interactionId,
    nativeSteps: steps,
    upstreamStateStored: Boolean((params.storeUpstreamState ?? true) && interactionId),
    previousAnchorRef: params.previousInteractionId,
    usage: {
      prompt_tokens:
        typeof usageMeta.total_input_tokens === "number"
          ? usageMeta.total_input_tokens
          : null,
      completion_tokens:
        typeof usageMeta.total_output_tokens === "number"
          ? usageMeta.total_output_tokens
          : null,
      total_tokens:
        typeof usageMeta.total_tokens === "number" ? usageMeta.total_tokens : null,
      cached_tokens:
        typeof usageMeta.total_cached_tokens === "number"
          ? usageMeta.total_cached_tokens
          : null,
      thinking_tokens:
        typeof usageMeta.total_thought_tokens === "number"
          ? usageMeta.total_thought_tokens
          : null,
      omitted_history_estimated_tokens: estimateOmittedHistoryTokens(
        params.omittedHistory
      ),
    },
    continuationUsed: Boolean(params.previousInteractionId),
    fallbackOccurred: false,
    fallbackReason: null,
  };
}

async function executeGeminiNative(
  params: ProviderExecutionInput
): Promise<ProviderExecutionResult> {
  const apiKey = config.geminiApiKey;
  if (!apiKey) {
    return {
      ok: false,
      httpStatus: 503,
      error: {
        source: "relay",
        code: "PROVIDER_CREDENTIALS_NOT_CONFIGURED",
        message:
          "GEMINI_API_KEY or GOOGLE_GEMINI_API_KEY is not configured on the server",
      },
    };
  }

  const ai = new GoogleGenAI({ apiKey });
  const contents: Content[] = params.history.map((turn) => ({
    role: turn.role,
    parts: [{ text: turn.text }],
  }));

  const currentParts: Part[] = [];
  for (const mat of params.materials) {
    const part = materialToGeminiPart(mat);
    if (part) currentParts.push(part);
  }
  currentParts.push({ text: extractInputText(params.input) });
  contents.push({ role: "user", parts: currentParts });

  const structured = buildStructuredOutputConfig(params.structuredOutput);
  const genConfig: Record<string, unknown> = {};
  if (params.instructions) genConfig.systemInstruction = params.instructions;
  if (typeof params.options.temperature === "number") {
    genConfig.temperature = params.options.temperature;
  }
  if (typeof params.options.top_p === "number") genConfig.topP = params.options.top_p;
  if (typeof params.options.max_output_tokens === "number") {
    genConfig.maxOutputTokens = params.options.max_output_tokens;
  }
  if (structured.mode === "json_schema" && structured.schema) {
    genConfig.responseMimeType = "application/json";
    genConfig.responseSchema = structured.schema;
  }

  try {
    const response = await ai.models.generateContent({
      model: params.route.upstream_model,
      contents,
      config: genConfig,
    });
    const text = response.text || "";
    const meta = response.usageMetadata;
    return {
      ok: true,
      text,
      rawText: text,
      model: params.route.upstream_model,
      interactionId: null,
      nativeSteps: [],
      upstreamStateStored: false,
      previousAnchorRef: null,
      usage: {
        prompt_tokens: meta?.promptTokenCount ?? null,
        completion_tokens: meta?.candidatesTokenCount ?? null,
        total_tokens: meta?.totalTokenCount ?? null,
        cached_tokens: meta?.cachedContentTokenCount ?? null,
        thinking_tokens: meta?.thoughtsTokenCount ?? null,
        omitted_history_estimated_tokens: 0,
      },
      continuationUsed: false,
      fallbackOccurred: false,
      fallbackReason: null,
    };
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return {
      ok: false,
      httpStatus: 502,
      error: {
        source: "upstream",
        code: "GEMINI_SDK_ERROR",
        message,
      },
    };
  }
}

async function executeOpenAICompatible(
  params: ProviderExecutionInput
): Promise<ProviderExecutionResult> {
  const isMoonshot = params.route.provider === "moonshot";
  const apiKey = isMoonshot ? config.moonshotApiKey : config.aihubmixApiKey;
  const baseUrl = isMoonshot ? config.moonshotBaseUrl : config.aihubmixOpenaiBaseUrl;

  if (!apiKey) {
    return {
      ok: false,
      httpStatus: 503,
      error: {
        source: "relay",
        code: "PROVIDER_CREDENTIALS_NOT_CONFIGURED",
        message: `${isMoonshot ? "MOONSHOT_API_KEY" : "AIHUBMIX_API_KEY"} is not configured on the server`,
      },
    };
  }

  const messages: Array<{ role: string; content: string }> = [];
  if (params.instructions) {
    messages.push({ role: "system", content: params.instructions });
  }
  for (const turn of params.history) {
    messages.push({
      role: turn.role === "model" ? "assistant" : "user",
      content: turn.text,
    });
  }
  const matText = params.materials
    .map((m) => {
      const obj = store.getObject(m.object_id);
      if (!obj) return `[Material: ${m.filename}]`;
      if (
        m.content_type.startsWith("text/") ||
        m.content_type === "application/json"
      ) {
        return `[Material: ${m.filename}]\n${obj.data.toString("utf8")}`;
      }
      return `[Material: ${m.filename} (${m.content_type}, ${m.size_bytes} bytes)]`;
    })
    .join("\n\n");
  const userText = [matText, extractInputText(params.input)]
    .filter(Boolean)
    .join("\n\n");
  messages.push({ role: "user", content: userText });

  const body: Record<string, unknown> = {
    model: params.route.upstream_model,
    messages,
  };
  if (typeof params.options.temperature === "number") {
    body.temperature = params.options.temperature;
  }
  if (typeof params.options.top_p === "number") body.top_p = params.options.top_p;
  if (typeof params.options.max_output_tokens === "number") {
    body.max_tokens = params.options.max_output_tokens;
  }

  const resp = await fetch(`${baseUrl}/chat/completions`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${apiKey}`,
    },
    body: JSON.stringify(body),
  });

  const rawBuf = Buffer.from(await resp.arrayBuffer());
  const rawText = rawBuf.toString("utf8");
  if (!resp.ok) {
    return {
      ok: false,
      httpStatus: 502,
      error: {
        source: "upstream",
        code: "UPSTREAM_HTTP_ERROR",
        message: rawText,
        upstream_http_status: resp.status,
        body_text: rawText,
      },
      rawBytes: rawBuf,
      rawContentType: resp.headers.get("content-type") || "application/json",
    };
  }

  const data = JSON.parse(rawText) as {
    model?: string;
    choices?: Array<{ message?: { content?: string } }>;
    usage?: {
      prompt_tokens?: number;
      completion_tokens?: number;
      total_tokens?: number;
    };
  };
  const text = data.choices?.[0]?.message?.content || "";
  return {
    ok: true,
    text,
    rawText: text,
    model: data.model || params.route.upstream_model,
    interactionId: null,
    nativeSteps: [],
    upstreamStateStored: false,
    previousAnchorRef: null,
    usage: {
      prompt_tokens: data.usage?.prompt_tokens ?? null,
      completion_tokens: data.usage?.completion_tokens ?? null,
      total_tokens: data.usage?.total_tokens ?? null,
      cached_tokens: null,
      thinking_tokens: null,
      omitted_history_estimated_tokens: 0,
    },
    continuationUsed: false,
    fallbackOccurred: false,
    fallbackReason: null,
  };
}

/**
 * Controlled single-dispatch provider invoker.
 * Strict architecture redline: zero same-turn anchor stripping retry and zero cross-protocol
 * fallback after dispatch_started.
 */
export async function executeProviderCall(
  params: ProviderExecutionInput
): Promise<ProviderExecutionResult> {
  if (params.route.protocol === "gemini_interactions") {
    return executeGeminiInteractions(params);
  }
  if (params.route.protocol === "gemini_native") {
    return executeGeminiNative(params);
  }
  return executeOpenAICompatible(params);
}
