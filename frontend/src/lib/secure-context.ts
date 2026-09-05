/**
 * Is this request actually reaching us over HTTPS?
 *
 * Three separate security controls need this answer, and all three originally
 * asked `process.env.NODE_ENV === "production"` instead:
 *
 * * `Strict-Transport-Security` (next.config.ts)
 * * `upgrade-insecure-requests` (middleware.ts)
 * * the `Secure` flag on the access cookie (api/access/{login,logout})
 *
 * `NODE_ENV` is the wrong question. **`next start` sets it to "production"**,
 * and `next start` is the documented way to run this application locally,
 * because `next dev` breaks under the nonce CSP. So every local run behaved as
 * though it were served over HTTPS.
 *
 * Chrome hides the consequences by treating loopback as a trustworthy origin:
 * it ignores HSTS there, ignores `upgrade-insecure-requests`, and stores
 * `Secure` cookies anyway. **Safari does none of those things.** The same build
 * that worked perfectly in Chrome rendered as unstyled HTML in Safari (every
 * asset upgraded to `https://127.0.0.1` and refused), and — once that was
 * fixed — accepted the passcode, discarded the `Secure` cookie, and bounced
 * straight back to the sign-in screen with no error anywhere.
 *
 * Two of those three failure modes are silent. Hence one helper, asked of the
 * request rather than of the build.
 */

/** Hostnames the browser treats as a trustworthy origin over plain HTTP. */
const LOOPBACK = new Set(["localhost", "127.0.0.1", "[::1]", "::1"]);

/** True when `host` is loopback, with any port stripped. */
export function isLoopbackHost(host: string | null | undefined): boolean {
  if (!host) return false;
  return LOOPBACK.has(withoutPort(host.trim().toLowerCase()));
}

/**
 * Strip a trailing `:port`, correctly for IPv6.
 *
 * A naive `/:\d+$/` turns `::1` into `:` — every colon in an IPv6 literal looks
 * like a port separator. So: a bracketed literal keeps its brackets, a name
 * with exactly one colon loses the port, and anything with more colons is an
 * unbracketed IPv6 address and is left alone.
 */
function withoutPort(host: string): string {
  if (host.startsWith("[")) {
    const close = host.indexOf("]");
    return close === -1 ? host : host.slice(0, close + 1);
  }
  const colons = host.split(":").length - 1;
  return colons === 1 ? host.replace(/:\d+$/, "") : host;
}

/**
 * True when the response will travel over HTTPS.
 *
 * `x-forwarded-proto` is consulted first because a platform terminates TLS in
 * front of this process: on Vercel the request arrives over HTTP internally
 * while the browser is on HTTPS, so the URL's own protocol would say "http" and
 * strip the `Secure` flag from a production cookie.
 *
 * The header is only trusted for non-loopback hosts. A request to
 * `localhost` carrying `x-forwarded-proto: https` is someone's curl, not a
 * proxy, and honouring it would put the local run straight back into the
 * failure this exists to prevent.
 */
export function isSecureRequest(request: {
  headers: { get(name: string): string | null };
  url: string;
}): boolean {
  // `Host` first: it is what the browser asked for. `request.url` can carry the
  // internal address a platform dialled rather than the public one. Typed to a
  // plain `Request` so middleware's `NextRequest` and a route handler's
  // `Request` both satisfy it -- the logout route receives the latter.
  const host = request.headers.get("host") ?? safeHost(request.url);
  if (isLoopbackHost(host)) return false;

  const forwarded = request.headers.get("x-forwarded-proto");
  if (forwarded) return forwarded.split(",")[0]?.trim() === "https";
  return request.url.startsWith("https:");
}

function safeHost(url: string): string | null {
  try {
    return new URL(url).host;
  } catch {
    return null;
  }
}
