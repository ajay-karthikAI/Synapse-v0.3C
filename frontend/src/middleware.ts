import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import { isLoopbackHost, isSecureRequest } from "@/lib/secure-context";
import { ACCESS_COOKIE, verifyAccessToken } from "@/lib/session";

/**
 * Two jobs on every request: set a nonce-based CSP, and decide whether the
 * caller has a valid session.
 *
 * ## The CSP
 *
 * A fresh 128-bit nonce per response, passed to the app through a request
 * header so `layout.tsx` can put it on Next's own scripts. `'strict-dynamic'`
 * means a script the nonced bundle loads is trusted transitively, which is what
 * lets Next's chunk loading work without `'unsafe-inline'`.
 *
 * `connect-src 'self'` is doing real work here: it is what stops a script that
 * somehow got onto the page from exfiltrating to another origin. The browser
 * only ever talks to this server; the backend is reached through the proxy.
 *
 * `style-src` allows `'unsafe-inline'`, which is a genuine and stated
 * weakening. Next injects inline styles during hydration and there is no nonce
 * hook for them. Style injection is a far smaller hazard than script injection,
 * and the alternative — dropping the CSP entirely — is worse.
 *
 * ## The session check
 *
 * Middleware decides *routing*, never authorisation. It redirects a caller
 * without a valid cookie away from a protected page so they see the passcode
 * screen instead of an empty shell. It is not the security boundary: the API
 * routes verify independently, and the backend verifies again. A middleware
 * matcher that misses a path must be a cosmetic bug, not a hole.
 */

/** Pages a caller may reach with no session at all. */
const PUBLIC_PATHS = new Set(["/access", "/transparency"]);

/** Where an unauthenticated caller is sent. */
const ACCESS_PATH = "/access";

function buildCsp(nonce: string, isProduction: boolean, upgradeInsecure: boolean): string {
  const directives = [
    "default-src 'self'",
    // `'strict-dynamic'` makes the nonce transitive to chunks Next loads.
    // The http: and https: entries are ignored by browsers that honour
    // strict-dynamic and act as the fallback for those that do not.
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic' https: ${
      isProduction ? "" : "'unsafe-eval'"
    }`.trim(),
    // See the module docstring: stated weakening, not an oversight.
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    // The one that matters most: the page talks to this origin and nowhere else.
    "connect-src 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "object-src 'none'",
    "worker-src 'self' blob:",
    "manifest-src 'self'",
  ];
  // Sent only where the app is actually reachable over HTTPS.
  //
  // `NODE_ENV === "production"` is NOT that test. `next start` sets it too, and
  // `next start` is the documented way to run this locally, because `next dev`
  // breaks under the nonce CSP. Chrome exempts loopback from this directive;
  // Safari does not — so running locally, Safari upgraded every stylesheet,
  // script and font to `https://127.0.0.1:3210`, got an SSL error on each, and
  // rendered the page as unstyled HTML while Chrome looked perfect.
  if (upgradeInsecure) directives.push("upgrade-insecure-requests");
  return directives.join("; ");
}

/**
 * Two years, and it applies to the whole host regardless of port.
 *
 * Emitted only when the response is genuinely travelling over HTTPS. Sent over
 * plain HTTP to a loopback address, Safari records the pin anyway and every
 * subsequent local run is upgraded to `https://127.0.0.1`, where nothing is
 * listening. Chrome ignores it there, which is what let this ship unnoticed.
 */
const HSTS = "max-age=63072000; includeSubDomains; preload";

export async function middleware(request: NextRequest) {
  const isProduction = process.env.NODE_ENV === "production";
  const secure = isSecureRequest(request);
  // Decided from the request, so it is right without anyone configuring it.
  const upgradeInsecure = isProduction && !isLoopbackHost(request.nextUrl.host);

  // 16 random bytes, base64. `crypto` is Web Crypto on the Edge runtime.
  const nonce = Buffer.from(crypto.getRandomValues(new Uint8Array(16))).toString("base64");
  const csp = buildCsp(nonce, isProduction, upgradeInsecure);

  /** Every response leaves through here, so neither header can be missed. */
  const harden = <T extends { headers: Headers }>(response: T): T => {
    response.headers.set("content-security-policy", csp);
    if (secure) response.headers.set("strict-transport-security", HSTS);
    return response;
  };

  const { pathname } = request.nextUrl;
  const token = request.cookies.get(ACCESS_COOKIE)?.value;
  const session = await verifyAccessToken(token, process.env.SYNAPSE_JWT_SECRET ?? "");

  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("content-security-policy", csp);

  // API routes are never redirected. They do their own verification and answer
  // with a typed JSON refusal; bouncing one to an HTML sign-in page would give
  // a `fetch` caller a 307 and a document where it expected `{code, message}`,
  // and a streaming client would see a redirect mid-request rather than an
  // envelope. Middleware still sets the CSP on them.
  const isApi = pathname.startsWith("/api/");
  const isPublic = isApi || PUBLIC_PATHS.has(pathname);

  // No session, protected page: send them to the passcode screen. `next` is
  // carried so they land where they were going, and it is validated on the
  // other side — an absolute URL here would be an open redirect.
  if (!session && !isPublic) {
    const url = request.nextUrl.clone();
    url.pathname = ACCESS_PATH;
    url.search = pathname === "/" ? "" : `?next=${encodeURIComponent(pathname)}`;
    const redirect = harden(NextResponse.redirect(url));
    // A cookie that failed verification is cleared rather than left to fail
    // again on every subsequent request.
    if (token) redirect.cookies.delete(ACCESS_COOKIE);
    return redirect;
  }

  // Already signed in and asking for the passcode screen: send them on.
  if (session && pathname === ACCESS_PATH) {
    const url = request.nextUrl.clone();
    url.pathname = "/";
    url.search = "";
    return harden(NextResponse.redirect(url));
  }

  return harden(NextResponse.next({ request: { headers: requestHeaders } }));
}

export const config = {
  matcher: [
    /*
     * Everything except Next's own static output and the icon. Notably this
     * DOES include /api/*, so the proxy routes get the CSP too — they return
     * JSON, but a header that is set everywhere cannot be forgotten somewhere.
     */
    {
      source: "/((?!_next/static|_next/image|favicon.ico).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
