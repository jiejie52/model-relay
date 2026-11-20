import express, { type NextFunction, type Request, type Response } from "express";
import { CAPABILITY_PROFILE_REVISION, config } from "./config.js";
import {
  CONTROL_PLANE_HASH,
  getConfigurationReport,
  getRegisteredProviders,
  ROUTE_CATALOG_HASH,
} from "./controlPlane.js";
import { requireRelayAuth } from "./middleware/auth.js";
import { v1Router } from "./routes/v1.js";
import { v2Router } from "./routes/v2.js";

const app = express();
app.use(express.json({ limit: "25mb" }));

app.get("/", (_req: Request, res: Response) => {
  res.json({
    ok: true,
    service: "relay-api",
    version: config.version,
    health: "/health",
    endpoints: [
      "GET /health",
      "GET /configuration",
      "POST /v2/materials",
      "GET /v2/materials/:materialId",
      "POST /v2/sessions",
      "GET /v2/sessions/:sessionId",
      "POST /v2/sessions/:sessionId/requests",
      "GET /v2/sessions/:sessionId/requests/:requestId",
      "GET /v2/sessions/:sessionId/requests/:requestId/result",
      "POST /v2/sessions/:sessionId/requests/:requestId/cancel",
      "POST /v1/dify/relay",
      "POST /v1/jobs",
      "GET /v1/jobs/:jobId",
      "GET /v1/jobs/:jobId/result",
      "POST /v1/jobs/:jobId/cancel",
    ],
  });
});

app.get("/health", (_req: Request, res: Response) => {
  res.json({
    ok: true,
    service: "relay-api",
    configuration: {
      status: "ready",
      persistence: "in_memory",
    },
    version: config.version,
    build_git_sha: config.buildGitSha,
    deployment_id: config.deploymentId,
    execution_pool: config.executionPool,
    route_revision: config.routeRevision,
    route_catalog_hash: ROUTE_CATALOG_HASH,
    control_plane_revision: config.modelControlPlaneRevision,
    control_plane_status: "ready",
    control_plane_hash: CONTROL_PLANE_HASH,
    providers: getRegisteredProviders(),
    capability_revision: CAPABILITY_PROFILE_REVISION,
  });
});

app.get("/configuration", requireRelayAuth, (_req: Request, res: Response) => {
  res.json(getConfigurationReport());
});

app.use("/v2", v2Router);
app.use("/v1", v1Router);

app.use((_req: Request, res: Response) => {
  res.status(404).json({ error: "Not found" });
});

app.use((err: unknown, _req: Request, res: Response, _next: NextFunction) => {
  const message = err instanceof Error ? err.message : "Internal server error";
  res.status(500).json({ error: message });
});

app.listen(config.port, config.host, () => {
  console.log(
    `[model-relay] listening on http://${config.host}:${config.port} (version ${config.version})`
  );
  if (!config.relayApiToken) {
    console.warn("[model-relay] RELAY_API_TOKEN is not set — auth running in permissive dev mode");
  }
});
