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
export const HETZNER_URL = "http://49.12.72.66:8000";
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
 * Get the API key from environment or options.
 */
function getApiKey(optionsKey?: string): string {
  const key = optionsKey ?? process.env.AI_BRIDGE_API_KEY;
  if (!key) {
    throw new Error(
      "[ai-bridge-sdk] AI_BRIDGE_API_KEY not set. Set it in environment or pass via options."
    );
  }
  return key;
}

/**
 * Create an OpenAI-compatible client connected to AI-Bridge.
 *
 * @param options.fallbackEnabled - Enable fallback to local
 * @param options.apiKey - API key (reads from AI_BRIDGE_API_KEY env var if not provided)
 * @returns OpenAI client configured for AI-Bridge
 * @throws AIBridgeConnectionError if no instance is reachable
 * @throws Error if API key is not configured
 */
export async function createClient(
  options: CreateClientOptions = {}
): Promise<OpenAI> {
  const baseUrl = await getBridgeUrl({
    fallbackEnabled: options.fallbackEnabled,
  });

  const apiKey = getApiKey(options.apiKey);

  return new OpenAI({
    baseURL: `${baseUrl}/v1`,
    apiKey: apiKey,
  });
}

// ============================================
// CACHED URL FOR SYNC ACCESS
// ============================================

let cachedUrl: string | null = null;

/**
 * Initialize and cache the bridge URL.
 * Call this once at app startup, then use getBridgeUrlSync() for sync access.
 *
 * @example
 * // In app initialization (e.g., _app.tsx or server startup)
 * await initializeBridgeUrl({ fallbackEnabled: true });
 *
 * // Later, in sync code (e.g., constructors)
 * const url = getBridgeUrlSync();
 */
export async function initializeBridgeUrl(
  options: GetBridgeUrlOptions = {}
): Promise<string> {
  cachedUrl = await getBridgeUrl(options);
  return cachedUrl;
}

/**
 * Get the cached bridge URL synchronously.
 * Requires initializeBridgeUrl() to be called first.
 *
 * @throws Error if not initialized
 */
export function getBridgeUrlSync(): string {
  if (!cachedUrl) {
    throw new Error(
      "[ai-bridge-sdk] Not initialized. Call await initializeBridgeUrl() first."
    );
  }
  return cachedUrl;
}

/**
 * Check if the SDK has been initialized.
 */
export function isInitialized(): boolean {
  return cachedUrl !== null;
}

/**
 * Get cached URL or fallback to default (for backwards compatibility).
 * Use this when you can't guarantee initialization but want a sensible default.
 */
export function getBridgeUrlOrDefault(): string {
  return cachedUrl ?? HETZNER_URL;
}

// Default export for convenience
export default {
  getBridgeUrl,
  getBridgeUrlSync,
  getBridgeUrlOrDefault,
  initializeBridgeUrl,
  isInitialized,
  createClient,
  healthCheck,
  HETZNER_URL,
  LOCAL_URL,
  AIBridgeConnectionError,
};
