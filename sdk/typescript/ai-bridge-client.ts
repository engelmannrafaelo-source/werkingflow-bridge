/**
 * AI-Bridge Client SDK
 * ====================
 *
 * Smart URL resolution with optional fallback from Hetzner to local.
 *
 * Usage:
 *   import { getBridgeUrl, createClient } from './ai-bridge-client';
 *
 *   // Simple (Hetzner only, fail if unavailable)
 *   const url = await getBridgeUrl();
 *
 *   // With fallback to local
 *   const url = await getBridgeUrl({ fallbackEnabled: true });
 *
 *   // Create OpenAI-compatible client
 *   const client = await createClient({ fallbackEnabled: true });
 *
 * Environment Variables:
 *   WRAPPER_URL: Override URL (disables fallback logic)
 *   AI_BRIDGE_FALLBACK: Enable fallback globally ("true" or "1")
 */

import OpenAI from "openai";

// Constants
export const HETZNER_URL = "http://95.217.180.242:8000";
export const LOCAL_URL = "http://localhost:8000";
const HETZNER_TIMEOUT = 3000; // ms
const LOCAL_TIMEOUT = 1000; // ms

export class AIBridgeConnectionError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "AIBridgeConnectionError";
  }
}

interface GetBridgeUrlOptions {
  fallbackEnabled?: boolean;
}

interface CreateClientOptions extends GetBridgeUrlOptions {
  apiKey?: string;
}

/**
 * Check if AI-Bridge is reachable at the given URL.
 */
export async function healthCheck(
  url: string,
  timeout: number
): Promise<boolean> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeout);

  try {
    const response = await fetch(`${url}/health`, {
      signal: controller.signal,
    });
    return response.ok;
  } catch {
    return false;
  } finally {
    clearTimeout(timeoutId);
  }
}

/**
 * Check if fallback is enabled via environment variable.
 */
function isFallbackEnabledEnv(): boolean {
  const value = (process.env.AI_BRIDGE_FALLBACK || "").toLowerCase();
  return ["true", "1", "yes"].includes(value);
}

/**
 * Resolve the AI-Bridge URL with optional fallback.
 *
 * Priority:
 * 1. WRAPPER_URL env var (explicit override, no fallback)
 * 2. Hetzner (if reachable)
 * 3. localhost:8000 (if fallback enabled and Hetzner unavailable)
 * 4. Throw AIBridgeConnectionError (if nothing reachable)
 *
 * @param options.fallbackEnabled - Enable fallback to local
 * @returns The resolved AI-Bridge URL
 * @throws AIBridgeConnectionError if no instance is reachable
 */
export async function getBridgeUrl(
  options: GetBridgeUrlOptions = {}
): Promise<string> {
  // Check for explicit URL override
  const overrideUrl = process.env.WRAPPER_URL;
  if (overrideUrl) {
    console.info(`[ai-bridge-sdk] Using override URL: ${overrideUrl}`);
    return overrideUrl;
  }

  // Determine if fallback is enabled
  const fallbackEnabled = options.fallbackEnabled ?? isFallbackEnabledEnv();

  // Try Hetzner first
  if (await healthCheck(HETZNER_URL, HETZNER_TIMEOUT)) {
    console.info(`[ai-bridge-sdk] Using Hetzner: ${HETZNER_URL}`);
    return HETZNER_URL;
  }

  console.warn(`[ai-bridge-sdk] Hetzner unavailable: ${HETZNER_URL}`);

  // Try local fallback if enabled
  if (fallbackEnabled) {
    if (await healthCheck(LOCAL_URL, LOCAL_TIMEOUT)) {
      console.warn(`[ai-bridge-sdk] Fallback to local: ${LOCAL_URL}`);
      return LOCAL_URL;
    }
    console.error(`[ai-bridge-sdk] Local also unavailable: ${LOCAL_URL}`);
  }

  // Nothing reachable - fail loud
  let msg = `AI-Bridge not reachable. Hetzner: ${HETZNER_URL}, Local: ${LOCAL_URL}`;
  if (!fallbackEnabled) {
    msg += " (fallback disabled, set AI_BRIDGE_FALLBACK=true to enable)";
  }
  throw new AIBridgeConnectionError(msg);
}

/**
 * Create an OpenAI-compatible client connected to AI-Bridge.
 *
 * @param options.fallbackEnabled - Enable fallback to local
 * @param options.apiKey - API key (default: "not-required")
 * @returns OpenAI client configured for AI-Bridge
 * @throws AIBridgeConnectionError if no instance is reachable
 */
export async function createClient(
  options: CreateClientOptions = {}
): Promise<OpenAI> {
  const baseUrl = await getBridgeUrl({
    fallbackEnabled: options.fallbackEnabled,
  });

  return new OpenAI({
    baseURL: `${baseUrl}/v1`,
    apiKey: options.apiKey ?? "not-required",
  });
}

// Default export for convenience
export default {
  getBridgeUrl,
  createClient,
  healthCheck,
  HETZNER_URL,
  LOCAL_URL,
  AIBridgeConnectionError,
};
