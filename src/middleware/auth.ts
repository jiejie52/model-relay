import crypto from "node:crypto";
import type { NextFunction, Request, Response } from "express";
import { config } from "../config.js";

function timingSafeMatch(provided: string, expected: string): boolean {
  const a = Buffer.from(provided, "utf8");
  const b = Buffer.from(expected, "utf8");
  if (a.length !== b.length) return false;
  return crypto.timingSafeEqual(a, b);
}

export function requireRelayAuth(req: Request, res: Response, next: NextFunction): void {
  if (!config.relayApiToken) {
    // Allow local/preview requests when RELAY_API_TOKEN is not configured.
    next();
    return;
  }

  const authHeader = req.header("authorization") || "";
  const match = /^Bearer\s+(.+)$/i.exec(authHeader.trim());
  if (!match) {
    res
      .status(401)
      .setHeader("WWW-Authenticate", "Bearer")
      .json({ detail: "Missing Relay bearer token" });
    return;
  }

  const provided = match[1].trim();
  if (!provided || !timingSafeMatch(provided, config.relayApiToken)) {
    res
      .status(401)
      .setHeader("WWW-Authenticate", "Bearer")
      .json({ detail: "Invalid Relay bearer token" });
    return;
  }

  next();
}

export interface OwnerHeaders {
  tenantId: string;
  conversationHash: string;
}

export function extractOwnerHeaders(req: Request): OwnerHeaders | null {
  const tenantId = req.header("x-tenant-id")?.trim();
  const conversationHash = req.header("x-conversation-hash")?.trim();
  if (!tenantId || !conversationHash) {
    return null;
  }
  return { tenantId, conversationHash };
}

export function requireOwnerHeaders(req: Request, res: Response, next: NextFunction): void {
  const owner = extractOwnerHeaders(req);
  if (!owner) {
    res.status(400).json({ detail: "X-Tenant-Id and X-Conversation-Hash are required" });
    return;
  }
  (req as Request & { relayOwner?: OwnerHeaders }).relayOwner = owner;
  next();
}
