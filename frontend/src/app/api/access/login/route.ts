import { NextResponse } from "next/server";

import { callBackend } from "@/lib/backend";
import { ACCESS_TTL_SECONDS, IS_PRODUCTION, isConfigured } from "@/lib/env";
import { ACCESS_COOKIE } from "@/lib/session";

/**
 * Exchange a passcode for a session.
 *
 * The passcode arrives from the browser, is forwarded once to FastAPI with the
 * service token, and is not retained. The backend does the constant-time
 * comparison and the rate limiting; this route adds the service token the
 * browser cannot hold and sets the cookie the browser cannot forge.
 *
 * The cookie is the backend's own token, forwarded unchanged. This server does
 * not mint a second one — one token, one secret, one expiry. See
 * `src/lib/session.ts`.
 */

// The proxy talks to a private address and streams; it must not be prerendered
// or cached at any layer.
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const GENERIC = { code: "unauthorized", message: "That passcode was not accepted." };

export async function POST(request: Request) {
  if (!isConfigured()) {
    // A misconfigured deployment must not look like a wrong passcode.
    return NextResponse.json(
      { code: "configuration_error", message: "This deployment is not configured." },
      { status: 503 },
    );
  }

  let passcode = "";
  let next = "/";
  try {
    const body: unknown = await request.json();
    if (typeof body === "object" && body !== null) {
      const record = body as Record<string, unknown>;
      passcode = typeof record["passcode"] === "string" ? record["passcode"] : "";
      const requested = typeof record["next"] === "string" ? record["next"] : "/";
      // Relative single-slash paths only. See `src/app/access/page.tsx`.
      next = requested.startsWith("/") && !requested.startsWith("//") ? requested : "/";
    }
  } catch {
    return NextResponse.json(GENERIC, { status: 400 });
  }

  if (!passcode) return NextResponse.json(GENERIC, { status: 400 });

  const upstream = await callBackend({
    path: "/v1/access/login",
    method: "POST",
    headers: new Headers({ "content-type": "application/json" }),
    body: JSON.stringify({ passcode }),
  });

  if (!upstream.ok) {
    // 429 is passed through because the remedy differs: wait, do not retry.
    // Everything else collapses to the same refusal.
    const status = upstream.status === 429 ? 429 : 401;
    return NextResponse.json(
      status === 429
        ? { code: "rate_limited", message: "Too many attempts. Please wait and try again." }
        : GENERIC,
      { status },
    );
  }

  // The signed token, taken from the backend's Set-Cookie or its body. The body
  // is not used to carry the token to the browser; it never leaves this server
  // except as an HttpOnly cookie.
  const token = extractToken(upstream);
  if (!token) {
    return NextResponse.json(
      { code: "internal_error", message: "Sign-in could not be completed." },
      { status: 502 },
    );
  }

  const response = NextResponse.json({ next });
  response.cookies.set({
    name: ACCESS_COOKIE,
    value: token,
    httpOnly: true, // Unreadable to page JavaScript
    // Off over plain HTTP in local development, where the browser would
    // otherwise refuse to store it and sign-in would silently never work.
    secure: IS_PRODUCTION,
    sameSite: "lax", // Not sent on a cross-site POST
    path: "/",
    maxAge: ACCESS_TTL_SECONDS,
  });
  return response;
}

/** Pull the access token out of the backend's response. */
function extractToken(upstream: Response): string | null {
  const setCookie = upstream.headers.get("set-cookie");
  if (setCookie) {
    const match = new RegExp(`${ACCESS_COOKIE}=([^;]+)`).exec(setCookie);
    if (match?.[1]) return match[1];
  }
  return null;
}
