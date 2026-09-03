import "server-only";

import { API_URL, SERVICE_TOKEN } from "@/lib/env";
import { ACCESS_COOKIE } from "@/lib/session";

/**
 * The only place this application talks to FastAPI.
 *
 * The rule that shapes everything here: **headers are constructed, never
 * copied.** An incoming request from a browser is attacker-influenced in its
 * entirety. Forwarding it — even "just the safe ones" — is how a caller ends up
 * choosing its own `X-Service-Token`, its own `X-Forwarded-For` (which the
 * backend's login limiter keys on), or a `Host` that reroutes the request.
 *
 * So `buildBackendHeaders` starts from an EMPTY `Headers` and adds only what it
 * decides. There is no allow-list to get wrong, because there is no copying.
 *
 * The service token lives here and only here. The browser never receives it;
 * that is what makes the backend private even though it is reachable.
 */

/** Header names this server sets itself and must never accept from a client. */
export const OVERWRITTEN_HEADERS = [
  "authorization",
  "x-service-token",
  "cookie",
  "x-forwarded-for",
  "x-forwarded-host",
  "x-forwarded-proto",
  "host",
  "origin",
  "referer",
] as const;

/** Request headers that are safe to carry through, because the backend needs them. */
const FORWARDABLE = new Set(["content-type", "accept"]);

/** Response headers worth returning. Everything else is dropped. */
const RESPONSE_PASSTHROUGH = new Set([
  "content-type",
  "content-disposition",
  "content-length",
  "cache-control",
  "x-content-type-options",
  "x-accel-buffering",
]);

export interface BackendRequest {
  path: string;
  method: string;
  body?: BodyInit | null | undefined;
  /** The verified access token, when the route needs one. */
  accessToken?: string | undefined;
  headers?: Headers | undefined;
  signal?: AbortSignal | undefined;
}

/**
 * Build the outgoing header set from scratch.
 *
 * Exported so `tests/unit/proxy-headers.test.ts` can assert the property
 * directly: whatever a client sends, these are what the backend receives.
 */
export function buildBackendHeaders(
  incoming: Headers | undefined,
  accessToken: string | undefined,
): Headers {
  const headers = new Headers();

  // Only these, and only from a fixed set. Note this reads specific names
  // rather than iterating the incoming headers, so a header nobody thought
  // about cannot arrive by default.
  if (incoming) {
    for (const name of FORWARDABLE) {
      const value = incoming.get(name);
      if (value) headers.set(name, value);
    }
  }

  // Set by this server, from server-side configuration. A client-supplied value
  // of the same name was never read, so there is nothing to overwrite.
  headers.set("x-service-token", SERVICE_TOKEN);
  if (accessToken) {
    headers.set("authorization", `Bearer ${accessToken}`);
  }
  return headers;
}

/** Strip everything the browser has no business receiving from the backend. */
export function filterResponseHeaders(upstream: Headers): Headers {
  const headers = new Headers();
  for (const [name, value] of upstream.entries()) {
    if (RESPONSE_PASSTHROUGH.has(name.toLowerCase())) headers.set(name, value);
  }
  return headers;
}

/**
 * Call the backend. Returns the raw `Response` so a caller can stream it.
 *
 * `duplex: "half"` is required to send a streaming request body under undici.
 * `cache: "no-store"` because none of these responses may be cached: one is a
 * conversation turn.
 */
export async function callBackend(request: BackendRequest): Promise<Response> {
  const url = new URL(request.path, API_URL);
  return fetch(url, {
    method: request.method,
    headers: buildBackendHeaders(request.headers, request.accessToken),
    body: request.body ?? null,
    cache: "no-store",
    redirect: "manual",
    ...(request.signal ? { signal: request.signal } : {}),
    ...(request.body ? { duplex: "half" } : {}),
  } as RequestInit);
}

/** The cookie name, re-exported so route handlers need one import. */
export { ACCESS_COOKIE };
