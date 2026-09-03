import { jwtVerify } from "jose";

/**
 * Verifying the access cookie.
 *
 * The cookie is issued and signed by FastAPI (`synapse.api.routes.access`) and
 * forwarded by this server unchanged. There is deliberately only ONE token in
 * the system: minting a second Next.js-signed cookie would mean two secrets,
 * two expiries that can disagree, and a logout that has to clear both or
 * silently does not.
 *
 * `jose` rather than `jsonwebtoken` because middleware runs on the Edge
 * runtime, which has Web Crypto but not Node's `crypto`.
 *
 * Two rules, both matching the backend's decoder:
 *
 * 1. The algorithm is stated, never read from the token. Without that, a token
 *    whose header says `{"alg":"none"}` verifies.
 * 2. Every failure returns the same `null`. Expired, forged, malformed and
 *    absent are indistinguishable to the caller, because a caller that could
 *    tell them apart would eventually tell a client.
 */

/** Set by FastAPI. This server forwards it; it never mints its own. */
export const ACCESS_COOKIE = "synapse_access";

/** Matches `synapse.api.security.JWT_ALGORITHM`. */
const ALGORITHM = "HS256";

export interface AccessSession {
  /** The random session id the backend generated. Names nothing about a person. */
  sessionId: string;
  /** Expiry, in seconds since the epoch. */
  expiresAt: number;
}

/**
 * Verify a token and return its session, or `null` for anything invalid.
 *
 * Never throws. A verification failure is an expected state — a patient who
 * left a tab open overnight — not an exception.
 */
export async function verifyAccessToken(
  token: string | undefined,
  secret: string,
): Promise<AccessSession | null> {
  if (!token || !secret) return null;
  try {
    const { payload } = await jwtVerify(token, new TextEncoder().encode(secret), {
      algorithms: [ALGORITHM],
      // `jose` checks `exp` itself; requiring it means a token without one is
      // rejected rather than treated as eternal.
      requiredClaims: ["exp", "sid"],
    });
    const sessionId = payload["sid"];
    if (typeof sessionId !== "string" || sessionId.length === 0) return null;
    return { sessionId, expiresAt: typeof payload.exp === "number" ? payload.exp : 0 };
  } catch {
    // Every failure mode collapses to the same answer. See the module docstring.
    return null;
  }
}
