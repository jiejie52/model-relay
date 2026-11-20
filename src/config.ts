import "dotenv/config";

export const RELAY_VERSION = "10.3.0";
export const BUILD_GIT_SHA = process.env.BUILD_GIT_SHA || "unknown";
export const CAPABILITY_PROFILE_REVISION = "relay-capability-profile/2026-10-02.1";

function numEnv(name: string, fallback: number): number {
  const raw = process.env[name];
  if (!raw) return fallback;
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export const config = {
  appName: "model-relay",
  version: RELAY_VERSION,
  buildGitSha: BUILD_GIT_SHA,
  port: numEnv("PORT", 3000),
  host: "0.0.0.0",

  deploymentId: process.env.DEPLOYMENT_ID || "railway",
  executionPool: process.env.EXECUTION_POOL || "railway-default",
  routeRevision: process.env.ROUTE_REVISION || "relay-route-catalog/2026-09-27.1",
  modelControlPlaneRevision:
    process.env.MODEL_CONTROL_PLANE_REVISION || "relay-model-control-plane/2026-10-02.1",

  relayApiToken: process.env.RELAY_API_TOKEN || "",
  geminiApiKey: process.env.GEMINI_API_KEY || process.env.GOOGLE_GEMINI_API_KEY || "",
  aihubmixApiKey: process.env.AIHUBMIX_API_KEY || "",
  aihubmixOpenaiBaseUrl: (process.env.AIHUBMIX_OPENAI_BASE_URL || "https://aihubmix.com/v1").replace(
    /\/+$/,
    ""
  ),
  aihubmixClaudeBaseUrl: (process.env.AIHUBMIX_CLAUDE_BASE_URL || "https://aihubmix.com").replace(
    /\/+$/,
    ""
  ),
  aihubmixGeminiBaseUrl: (
    process.env.AIHUBMIX_GEMINI_BASE_URL || "https://generativelanguage.googleapis.com"
  ).replace(/\/+$/, ""),
  moonshotApiKey: process.env.MOONSHOT_API_KEY || "",
  moonshotBaseUrl: (process.env.MOONSHOT_BASE_URL || "https://api.moonshot.cn/v1").replace(
    /\/+$/,
    ""
  ),

  sessionTtlSeconds: numEnv("SESSION_TTL_SECONDS", 604800),
  jobTtlSeconds: numEnv("JOB_TTL_SECONDS", 604800),
  syncRequestDeadlineSeconds: numEnv("SYNC_REQUEST_DEADLINE_SECONDS", 120),
  materialIngressMaxBytes: numEnv("MATERIAL_INGRESS_MAX_BYTES", 104857600),
  materialIngressTimeoutSeconds: numEnv("MATERIAL_INGRESS_TIMEOUT_SECONDS", 120),
  materialAllowHttp: (process.env.MATERIAL_ALLOW_HTTP || "false").toLowerCase() === "true",
  materialDefaultDurabilityPolicy:
    process.env.MATERIAL_DEFAULT_DURABILITY_POLICY || "native_first",
  materialDefaultFallbackPolicy:
    process.env.MATERIAL_DEFAULT_FALLBACK_POLICY || "on_provider_unavailable",
  relayResultHardLimitBytes: numEnv("RELAY_RESULT_HARD_LIMIT_BYTES", 786432),
  relayResultPreviewBytes: numEnv("RELAY_RESULT_PREVIEW_BYTES", 307200),
  defaultStorageId: process.env.DEFAULT_STORAGE_ID || "memory_store",
};
