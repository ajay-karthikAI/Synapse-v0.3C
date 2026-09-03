// @vitest-environment node
//
// The node environment, not jsdom: this module runs in middleware and in
// route handlers, never in a browser. jsdom also supplies a cross-realm
// TextEncoder whose Uint8Array fails `instanceof` inside jose.
import { SignJWT } from "jose";
import { describe, expect, it } from "vitest";

import { verifyAccessToken } from "@/lib/session";

/**
 * Cookie verification.
 *
 * The properties here mirror the backend's decoder exactly: the algorithm is
 * stated rather than read from the token, and every failure returns the same
 * `null` so a caller cannot use the result as an oracle.
 */

const SECRET = "unit-test-jwt-secret-0123456789ab";

async function sign(
  claims: Record<string, unknown>,
  { secret = SECRET, expires = "8h" }: { secret?: string; expires?: string } = {},
): Promise<string> {
  return new SignJWT(claims)
    .setProtectedHeader({ alg: "HS256" })
    .setIssuedAt()
    .setExpirationTime(expires)
    .sign(new TextEncoder().encode(secret));
}

describe("a valid token", () => {
  it("returns the session id the backend put in it", async () => {
    const token = await sign({ sid: "a1b2c3d4e5f6" });
    await expect(verifyAccessToken(token, SECRET)).resolves.toMatchObject({
      sessionId: "a1b2c3d4e5f6",
    });
  });

  it("carries an expiry", async () => {
    const token = await sign({ sid: "abc" });
    const session = await verifyAccessToken(token, SECRET);
    expect(session?.expiresAt).toBeGreaterThan(Math.floor(Date.now() / 1000));
  });
});

describe("every invalid token is rejected identically", () => {
  it("rejects an absent token", async () => {
    await expect(verifyAccessToken(undefined, SECRET)).resolves.toBeNull();
  });

  it("rejects an empty token", async () => {
    await expect(verifyAccessToken("", SECRET)).resolves.toBeNull();
  });

  it("rejects a token signed with another secret", async () => {
    const token = await sign({ sid: "abc" }, { secret: "a-completely-different-secret-00" });
    await expect(verifyAccessToken(token, SECRET)).resolves.toBeNull();
  });

  it("rejects an expired token", async () => {
    const token = await sign({ sid: "abc" }, { expires: "-1h" });
    await expect(verifyAccessToken(token, SECRET)).resolves.toBeNull();
  });

  it("rejects a token with no session id", async () => {
    const token = await sign({ nothing: "useful" });
    await expect(verifyAccessToken(token, SECRET)).resolves.toBeNull();
  });

  it("rejects a token whose session id is not a string", async () => {
    const token = await sign({ sid: 12345 });
    await expect(verifyAccessToken(token, SECRET)).resolves.toBeNull();
  });

  it("rejects an unsigned alg:none token", async () => {
    // The classic JWT defect: a decoder that reads `alg` from the token itself
    // accepts this. `verifyAccessToken` states HS256 and never consults it.
    const header = Buffer.from(JSON.stringify({ alg: "none", typ: "JWT" })).toString("base64url");
    const payload = Buffer.from(
      JSON.stringify({ sid: "forged", exp: Math.floor(Date.now() / 1000) + 3600 }),
    ).toString("base64url");
    await expect(verifyAccessToken(`${header}.${payload}.`, SECRET)).resolves.toBeNull();
  });

  it("rejects garbage", async () => {
    for (const token of ["not-a-jwt", "a.b.c", "...", "{}", "   "]) {
      await expect(verifyAccessToken(token, SECRET)).resolves.toBeNull();
    }
  });

  it("rejects everything when no secret is configured", async () => {
    const token = await sign({ sid: "abc" });
    await expect(verifyAccessToken(token, "")).resolves.toBeNull();
  });
});

describe("the decoder is not an oracle", () => {
  it("returns an identical value for expired, forged and malformed", async () => {
    const expired = await sign({ sid: "abc" }, { expires: "-1h" });
    const forged = await sign({ sid: "abc" }, { secret: "another-secret-entirely-000000" });
    const results = await Promise.all([
      verifyAccessToken(expired, SECRET),
      verifyAccessToken(forged, SECRET),
      verifyAccessToken("garbage", SECRET),
      verifyAccessToken(undefined, SECRET),
    ]);
    // Not merely "all falsy" — identical, so nothing distinguishes them.
    expect(new Set(results)).toEqual(new Set([null]));
  });
});
