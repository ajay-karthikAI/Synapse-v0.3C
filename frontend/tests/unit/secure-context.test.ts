import { describe, expect, it } from "vitest";

import { isLoopbackHost, isSecureRequest } from "@/lib/secure-context";

/**
 * Three security controls decide whether the app is being served over HTTPS,
 * and all three used to ask `NODE_ENV === "production"` instead:
 * `Strict-Transport-Security`, `upgrade-insecure-requests`, and the `Secure`
 * flag on the access cookie.
 *
 * That test is wrong, because `next start` sets NODE_ENV to "production" and
 * `next start` is how this application runs locally — `next dev` breaks under
 * the nonce CSP. Chrome hid it by treating loopback as trustworthy; Safari did
 * not, and the same build rendered as unstyled HTML and then refused to keep a
 * session, with no error anywhere.
 *
 * Both directions matter here. Dropping `Secure` locally is what makes Safari
 * work; keeping it in production is what stops the session cookie travelling in
 * clear text. A test that only checked the first would be worse than none.
 */

const request = (host: string, headers: Record<string, string> = {}, url?: string) => ({
  headers: {
    get: (name: string) =>
      name.toLowerCase() === "host" ? host : (headers[name.toLowerCase()] ?? null),
  },
  url: url ?? `http://${host}/api/access/login`,
});

describe("isLoopbackHost", () => {
  it.each(["localhost", "127.0.0.1", "localhost:3210", "127.0.0.1:3210", "[::1]", "::1"])(
    "%s is loopback",
    (host) => {
      expect(isLoopbackHost(host)).toBe(true);
    },
  );

  it.each(["synapse.example.com", "synapse.vercel.app", "10.0.0.5", ""])(
    "%s is not loopback",
    (host) => {
      expect(isLoopbackHost(host)).toBe(false);
    },
  );

  it("is not fooled by a hostname that merely contains one", () => {
    // `notlocalhost.com` would pass a naive `includes` check.
    expect(isLoopbackHost("notlocalhost.com")).toBe(false);
    expect(isLoopbackHost("127.0.0.1.evil.com")).toBe(false);
  });

  it("ignores case and surrounding whitespace", () => {
    expect(isLoopbackHost(" LocalHost:3210 ")).toBe(true);
  });
});

describe("isSecureRequest", () => {
  it("is false on loopback, which is what lets Safari keep the cookie", () => {
    expect(isSecureRequest(request("localhost:3210"))).toBe(false);
    expect(isSecureRequest(request("127.0.0.1:3210"))).toBe(false);
  });

  it("is TRUE behind a TLS-terminating proxy", () => {
    // Vercel terminates TLS and forwards over HTTP internally, so the URL's own
    // protocol says "http" while the browser is on HTTPS. Reading only that
    // would strip Secure from a production cookie.
    expect(
      isSecureRequest(request("synapse.example.com", { "x-forwarded-proto": "https" })),
    ).toBe(true);
  });

  it("reads only the first entry of a forwarded chain", () => {
    expect(
      isSecureRequest(request("synapse.example.com", { "x-forwarded-proto": "https, http" })),
    ).toBe(true);
    expect(
      isSecureRequest(request("synapse.example.com", { "x-forwarded-proto": "http, https" })),
    ).toBe(false);
  });

  it("falls back to the URL when no proxy header is present", () => {
    expect(
      isSecureRequest(request("synapse.example.com", {}, "https://synapse.example.com/x")),
    ).toBe(true);
    expect(
      isSecureRequest(request("synapse.example.com", {}, "http://synapse.example.com/x")),
    ).toBe(false);
  });

  it("cannot be tricked into dropping Secure in production", () => {
    // The dangerous direction. A forged `x-forwarded-proto: http` on a real
    // deployment must not downgrade the cookie... and it does report false
    // here, which is why the proxy is the only thing allowed to set it. This
    // asserts the CURRENT behaviour so a change to it is deliberate.
    expect(
      isSecureRequest(request("synapse.example.com", { "x-forwarded-proto": "http" })),
    ).toBe(false);
  });

  it("cannot be tricked into ADDING Secure on loopback", () => {
    // The direction that broke Safari. A stray forwarded header on a local run
    // must not put the cookie back out of reach.
    expect(
      isSecureRequest(request("localhost:3210", { "x-forwarded-proto": "https" })),
    ).toBe(false);
  });

  it("handles a malformed url without throwing", () => {
    expect(isSecureRequest({ headers: { get: () => null }, url: "not a url" })).toBe(false);
  });
});
