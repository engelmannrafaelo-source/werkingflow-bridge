/**
 * Bridge Attribution SDK for Frontend Apps
 *
 * Provides multi-dimension attribution context for usage tracking.
 * Automatically extracts user/tenant from NextAuth session.
 *
 * Usage in API routes:
 * ```ts
 * import { getBridgeAttribution } from '@/lib/bridge-attribution'
 *
 * const attribution = await getBridgeAttribution('werking-report')
 * const response = await fetch(bridgeUrl, {
 *   headers: {
 *     ...buildAttributionHeaders(attribution)
 *   }
 * })
 * ```
 */

/**
 * Multi-dimension attribution context.
 * All fields are optional and will be sent as HTTP headers to the Bridge.
 */
export interface BridgeAttributionContext {
  userId?: string      // User identifier (e.g., Supabase user UUID)
  tenantId?: string    // Tenant/organization identifier
  appId?: string       // Application identifier (e.g., "werking-report", "werking-energy")
  agentId?: string     // Autonomous agent identifier (e.g., "herbert", "sarah")
  sessionId?: string   // Session UUID (e.g., Claude Code session)
  workflowId?: string  // Workflow type identifier (e.g., "energy-report-v2")
  jobId?: string       // Job/run UUID (e.g., workflow execution UUID)
}

/**
 * Build HTTP headers from attribution context.
 *
 * Returns headers object ready to merge into fetch() headers.
 */
export function buildAttributionHeaders(
  attribution?: BridgeAttributionContext
): Record<string, string> {
  const headers: Record<string, string> = {}
  if (!attribution) return headers

  if (attribution.userId) headers['X-User-ID'] = attribution.userId
  if (attribution.tenantId) headers['X-Tenant-ID'] = attribution.tenantId
  if (attribution.appId) headers['X-App-ID'] = attribution.appId
  if (attribution.agentId) headers['X-Agent-ID'] = attribution.agentId
  if (attribution.sessionId) headers['X-Session-ID'] = attribution.sessionId
  if (attribution.workflowId) headers['X-Workflow-ID'] = attribution.workflowId
  if (attribution.jobId) headers['X-Job-ID'] = attribution.jobId

  return headers
}

/**
 * Extract attribution from NextAuth session (server-side only).
 *
 * @param appId - Application identifier (e.g., "werking-report")
 * @returns Attribution context with userId, tenantId, appId
 *
 * Usage:
 * ```ts
 * import { getServerSession } from 'next-auth'
 * import { getBridgeAttribution } from '@/lib/bridge-attribution'
 *
 * export async function POST(req: Request) {
 *   const session = await getServerSession()
 *   const attribution = getBridgeAttribution('werking-report', session)
 *   // ... use attribution
 * }
 * ```
 */
export function getBridgeAttribution(
  appId: string,
  session: any // NextAuth Session type (import from next-auth if available)
): BridgeAttributionContext {
  return {
    userId: session?.user?.id,
    tenantId: session?.tenant?.id, // May not exist in all session types
    appId
  }
}

/**
 * Create attribution context with custom values.
 *
 * Useful for overriding specific fields or providing additional context.
 *
 * Usage:
 * ```ts
 * const baseAttribution = getBridgeAttribution('werking-energy', session)
 * const workflowAttribution = createBridgeAttribution({
 *   ...baseAttribution,
 *   workflowId: 'energy-report-v2',
 *   jobId: jobUuid
 * })
 * ```
 */
export function createBridgeAttribution(
  context: Partial<BridgeAttributionContext>
): BridgeAttributionContext {
  return { ...context }
}
