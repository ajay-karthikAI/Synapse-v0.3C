// @vitest-environment node
//
// The node environment, not jsdom: this module runs in middleware and in
// route handlers, never in a browser. jsdom also supplies a cross-realm
// TextEncoder whose Uint8Array fails `instanceof` inside jose.
import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The proxy's central security property: headers are BUILT, never forwarded.
 *
 * A browser request is attacker-influenced in its entirety. If any of it were
 * copied through, a caller could choose its own service token (reaching the
 * backend as this server), its own `Authorization` (reaching another session),
 * or its own `X-Forwarded-For` — which the backend's login limiter keys on, so
 * spoofing it would reset the rate-limit bucket at will.
 */

vi.mock("server-only", () => ({}));

const SERVICE_TOKEN = "the-real-service-token-0123456789";

beforeEach(() => {
  vi.resetModules();
  vi.stubEnv("SYNAPSE_SERVICE_TOKEN", SERVICE_TOKEN);
  vi.stubEnv("SYNAPSE_JWT_SECRET", "unit-test-jwt-secret-0123456789ab");
  vi.stubEnv("SYNAPSE_API_URL", "http://127.0.0.1:8000");
});

async function build(incoming: Record<string, string>, accessToken?: string) {
  const { buildBackendHeaders } = await import("@/lib/backend");
  return buildBackendHeaders(new Headers(incoming), accessToken);
}

describe("credentials are set by this server, never taken from the client", () => {
  it("sets the service token from configuration", async () => {
    const headers = await build({});
    expect(headers.get("x-service-token")).toBe(SERVICE_TOKEN);
  });

  it("ignores a client-supplied service token entirely", async () => {
    const headers = await build({ "x-service-token": "attacker-chosen-value" });
    expect(headers.get("x-service-token")).toBe(SERVICE_TOKEN);
  });

  it("ignores a client-supplied Authorization header", async () => {
    const headers = await build({ authorization: "Bearer someone-elses-token" });
    expect(headers.get("authorization")).toBeNull();
  });

  it("sets Authorization only from the verified session token", async () => {
    const headers = await build({ authorization: "Bearer forged" }, "verified-token");
    expect(headers.get("authorization")).toBe("Bearer verified-token");
  });
});

describe("nothing else crosses the boundary", () => {
  it.each([
    "cookie",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "origin",
    "referer",
    "user-agent",
    "x-real-ip",
    "forwarded",
  ])("drops %s", async (name) => {
    const headers = await build({ [name]: "attacker-controlled" });
    expect(headers.get(name)).toBeNull();
  });

  it("drops a header nobody thought about", async () => {
    // The construction reads a fixed list of names rather than iterating the
    // incoming headers, so an unknown header cannot arrive by default.
    const headers = await build({ "x-some-future-header": "surprise" });
    expect(headers.get("x-some-future-header")).toBeNull();
  });

  it("carries content-type and accept, which the backend needs", async () => {
    const headers = await build({
      "content-type": "application/json",
      accept: "text/event-stream",
    });
    expect(headers.get("content-type")).toBe("application/json");
    expect(headers.get("accept")).toBe("text/event-stream");
  });

  it("emits only the headers it decided on", async () => {
    const headers = await build(
      { cookie: "a=b", "content-type": "application/json", "user-agent": "x" },
      "token",
    );
    expect([...headers.keys()].sort()).toEqual([
      "authorization",
      "content-type",
      "x-service-token",
    ]);
  });
});

describe("responses are filtered on the way back", () => {
  it("keeps what a browser needs and drops the rest", async () => {
    const { filterResponseHeaders } = await import("@/lib/backend");
    const filtered = filterResponseHeaders(
      new Headers({
        "content-type": "text/event-stream",
        "x-accel-buffering": "no",
        "content-disposition": 'attachment; filename="brief.txt"',
        // Must not reach the browser: a backend Set-Cookie would let the API
        // set cookies on this origin.
        "set-cookie": "backend_session=leaked",
        server: "uvicorn",
        "x-internal-trace": "abc123",
      }),
    );
    expect(filtered.get("content-type")).toBe("text/event-stream");
    // Preserved so an intermediate proxy does not re-buffer the stream.
    expect(filtered.get("x-accel-buffering")).toBe("no");
    expect(filtered.get("content-disposition")).toContain("attachment");
    expect(filtered.get("set-cookie")).toBeNull();
    expect(filtered.get("server")).toBeNull();
    expect(filtered.get("x-internal-trace")).toBeNull();
  });
});

describe("the allow-list covers the paths this application actually calls", () => {
  /**
   * The prefix list and the client's paths are two lists that have to agree,
   * and nothing made them. When the brief moved from `v1/turns/{i}/brief` to
   * `v1/brief` it left every permitted prefix behind, and the proxy returned
   * its own 404 without ever calling the backend — a break that no unit test
   * on either side would have seen, because each side was correct alone.
   */
  async function prefixes() {
    const { ALLOWED_PREFIXES } = await import("@/lib/proxy-paths");
    return ALLOWED_PREFIXES as readonly string[];
  }

  function reachable(path: string, allowed: readonly string[]): boolean {
    return allowed.some((prefix) => path === prefix || path.startsWith(`${prefix}/`));
  }

  it("permits the session-wide brief", async () => {
    const { BRIEF_PATH } = await import("@/lib/session-client");
    expect(reachable(BRIEF_PATH, await prefixes())).toBe(true);
  });

  it("permits every brief edit and export path", async () => {
    const { BRIEF_PATH, EXPORT_FORMATS } = await import("@/lib/session-client");
    const allowed = await prefixes();
    const paths = [
      `${BRIEF_PATH}/topic`,
      `${BRIEF_PATH}/notes`,
      `${BRIEF_PATH}/questions`,
      `${BRIEF_PATH}/questions/order`,
      `${BRIEF_PATH}/questions/q1`,
      `${BRIEF_PATH}/sections`,
      ...EXPORT_FORMATS.map((format) => `${BRIEF_PATH}/export/${format}`),
    ];
    for (const path of paths) {
      expect(reachable(path, allowed), path).toBe(true);
    }
  });

  it("still refuses a path outside every prefix", async () => {
    // The allow-list has to stay an allow-list; widening it to fix the brief
    // must not have turned it into an open relay.
    const allowed = await prefixes();
    for (const path of ["v1/admin", "v1", "openapi.json", "docs", ""]) {
      expect(reachable(path, allowed), path).toBe(false);
    }
  });
});
