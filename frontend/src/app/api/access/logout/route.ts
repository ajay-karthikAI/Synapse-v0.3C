import { NextResponse } from "next/server";

import { callBackend } from "@/lib/backend";
import { ACCESS_COOKIE } from "@/lib/session";

/**
 * End the session.
 *
 * Two things must happen and the order matters: the backend discards the
 * conversation, and the browser loses the cookie. The cookie is cleared even if
 * the backend call fails — a caller who pressed "sign out" must end up signed
 * out of this browser whatever the network did.
 *
 * POST only. A GET logout is reachable by prefetch, by a crawler, and by any
 * page that can cause a navigation, which makes signing a patient out mid-turn
 * a cross-site request forgery.
 */

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  const token = readCookie(request.headers.get("cookie"), ACCESS_COOKIE);

  if (token) {
    try {
      await callBackend({ path: "/v1/access/logout", method: "POST", accessToken: token });
    } catch {
      // Deliberately swallowed. See the docstring: the cookie goes regardless.
    }
  }

  // A form POST expects a navigation back to a page; fetch callers get a 303
  // they can follow or ignore.
  //
  // The Location is RELATIVE, deliberately. `new URL("/access", request.url)`
  // resolves against the host the SERVER was configured with, not the host the
  // client actually used — behind a proxy that is an internal hostname, and the
  // patient is redirected somewhere they cannot reach. A relative reference is
  // valid per RFC 7231 §7.1.2 and is resolved by the browser against the origin
  // it is already on, so it cannot leak an internal address or become an open
  // redirect.
  const response = new NextResponse(null, {
    status: 303,
    headers: { location: "/access" },
  });
  response.cookies.set({
    name: ACCESS_COOKIE,
    value: "",
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    maxAge: 0,
  });
  return response;
}

function readCookie(header: string | null, name: string): string | undefined {
  if (!header) return undefined;
  for (const part of header.split(";")) {
    const [key, ...rest] = part.trim().split("=");
    if (key === name) return rest.join("=");
  }
  return undefined;
}
