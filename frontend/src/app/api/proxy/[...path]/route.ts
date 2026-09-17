import { NextResponse } from "next/server";

import { callBackend, filterResponseHeaders } from "@/lib/backend";
import { isConfigured } from "@/lib/env";
import { ALLOWED_PREFIXES } from "@/lib/proxy-paths";
import { ACCESS_COOKIE, verifyAccessToken } from "@/lib/session";

/**
 * The one door to the backend.
 *
 * Everything the browser needs from FastAPI comes through here, and the route
 * enforces three things the browser cannot be trusted to:
 *
 * 1. **Headers are built, not forwarded.** `callBackend` starts from an empty
 *    `Headers` (see `src/lib/backend.ts`). A client cannot supply its own
 *    `X-Service-Token`, its own `Authorization`, or its own `X-Forwarded-For` —
 *    which matters because the backend's login limiter keys on that last one.
 *
 * 2. **The path is an allow-list.** Only prefixes this application actually
 *    uses are reachable. Without it the proxy is an open relay to every
 *    endpoint the backend will ever have, including ones added later by someone
 *    who did not know this file existed.
 *
 * 3. **Streaming passes through untouched.** `upstream.body` is handed to the
 *    `Response` directly, so server-sent events arrive as they are produced.
 *    Buffering here would defeat the stage events entirely — and
 *    `X-Accel-Buffering: no` is preserved through the response filter so no
 *    intermediate proxy re-buffers what this one did not.
 */

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
// Streaming a turn can take up to the backend's 120-second deadline.
export const maxDuration = 150;

/** Routes that may be called without a session. */
const PUBLIC_PREFIXES = new Set<string>(["v1/transparency", "readyz"]);

const UNAUTHORIZED = { code: "unauthorized", message: "Not authorised." };

async function handle(request: Request, segments: string[]): Promise<Response> {
  if (!isConfigured()) {
    return NextResponse.json(
      { code: "configuration_error", message: "This deployment is not configured." },
      { status: 503 },
    );
  }

  // Rejoined from the matched segments rather than taken from the raw URL, so
  // `..`, encoded slashes and duplicate separators cannot reshape the path.
  const path = segments.filter((segment) => segment.length > 0).join("/");
  const allowed = ALLOWED_PREFIXES.find(
    (prefix) => path === prefix || path.startsWith(`${prefix}/`),
  );
  if (!allowed) {
    return NextResponse.json({ code: "not_found", message: "Unknown endpoint." }, { status: 404 });
  }

  let accessToken: string | undefined;
  if (!PUBLIC_PREFIXES.has(allowed)) {
    const cookie = readCookie(request.headers.get("cookie"), ACCESS_COOKIE);
    const session = await verifyAccessToken(cookie, process.env.SYNAPSE_JWT_SECRET ?? "");
    // Verified here as well as in middleware and again in the backend. Three
    // independent checks: middleware decides routing, this decides whether a
    // request is made at all, and the backend decides whether it is answered.
    if (!session) return NextResponse.json(UNAUTHORIZED, { status: 401 });
    accessToken = cookie;
  }

  const search = new URL(request.url).search;
  const hasBody = request.method !== "GET" && request.method !== "HEAD";

  // Buffered rather than forwarded as a stream, so the request can be sent
  // twice. Every client-to-server payload here is small JSON — a question is
  // capped at 2000 characters and a brief edit is one field — so there is no
  // upload to stream. The response is the streaming half, and it is untouched.
  const body = hasBody ? await request.arrayBuffer() : null;

  const send = () =>
    callBackend({
      path: `/${path}${search}`,
      method: request.method,
      headers: request.headers,
      body,
      accessToken,
      signal: request.signal,
    });

  let upstream: Response;
  try {
    upstream = await send();
  } catch {
    // One retry, and only for a connection that never produced a response.
    //
    // Node pools keep-alive sockets to the backend. When the backend restarts —
    // a deploy, a crash, an idle socket reaped at the far end — a pooled socket
    // is already dead, and the next request through it throws before a single
    // byte is sent. The retry opens a fresh connection and succeeds. Without
    // it, the first patient after every deploy is told the service is
    // unavailable when it is running perfectly.
    //
    // Safe to repeat because nothing here has been applied yet: the failure is
    // that the request never arrived. A turn additionally carries a
    // `client_request_id`, so even if the first attempt did reach the backend,
    // the second is replayed rather than run twice — which is exactly what that
    // idempotency key is for.
    if (request.signal.aborted) {
      return NextResponse.json(
        { code: "backend_unavailable", message: "The service is not available." },
        { status: 502 },
      );
    }
    try {
      upstream = await send();
    } catch {
      // A backend that cannot be reached is not an application crash. The type
      // is not reported: it would distinguish "refused" from "timed out" to a
      // caller.
      return NextResponse.json(
        { code: "backend_unavailable", message: "The service is not available." },
        { status: 502 },
      );
    }
  }

  // `upstream.body` passed straight through: this is what makes SSE stream.
  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: filterResponseHeaders(upstream.headers),
  });
}

export async function GET(request: Request, context: { params: Promise<{ path: string[] }> }) {
  return handle(request, (await context.params).path);
}

export async function POST(request: Request, context: { params: Promise<{ path: string[] }> }) {
  return handle(request, (await context.params).path);
}

export async function PUT(request: Request, context: { params: Promise<{ path: string[] }> }) {
  return handle(request, (await context.params).path);
}

export async function DELETE(request: Request, context: { params: Promise<{ path: string[] }> }) {
  return handle(request, (await context.params).path);
}

function readCookie(header: string | null, name: string): string | undefined {
  if (!header) return undefined;
  for (const part of header.split(";")) {
    const [key, ...rest] = part.trim().split("=");
    if (key === name) return rest.join("=");
  }
  return undefined;
}
