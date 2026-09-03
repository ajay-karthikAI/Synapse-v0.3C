import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { askTurn, newRequestId } from "@/lib/turns";

import { envelope } from "../fixtures/load";

/**
 * The turn transport: what the client does with each thing the wire can do.
 *
 * The distinction under test throughout is between an **outcome** and a
 * **fault**. A failure envelope is an outcome — the server decided something
 * and said so, and the patient should read it. A dropped connection is a fault,
 * and the patient should be able to retry it with their question intact. Code
 * that conflates the two either hides a real result behind "try again" or
 * offers a retry for something that will never succeed.
 */

/** A `Response` whose body is the given SSE text, delivered in one chunk. */
function sseResponse(body: string, status = 200): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(body));
      controller.close();
    },
  });
  return new Response(stream, {
    status,
    headers: { "content-type": "text/event-stream" },
  });
}

function frame(event: string, payload: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`;
}

const ask = () => askTurn({ query: "why is my blood sugar high?", clientRequestId: "req-1" });

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe("a successful turn", () => {
  it("returns the single envelope", async () => {
    const answer = envelope("answer");
    vi.mocked(fetch).mockResolvedValue(sseResponse(frame("envelope", answer)));

    const result = await ask();
    expect(result).toEqual({ ok: true, envelope: answer });
  });

  it("reports every stage in order, then the envelope", async () => {
    vi.mocked(fetch).mockResolvedValue(
      sseResponse(
        frame("stage", { stage: "checking_question", message: "Checking your question..." }) +
          frame("stage", { stage: "searching", message: "Searching relevant research..." }) +
          frame("envelope", envelope("answer")),
      ),
    );

    const stages: string[] = [];
    const result = await askTurn({
      query: "q",
      clientRequestId: "r",
      onStage: (stage) => stages.push(stage.stage),
    });

    expect(stages).toEqual(["checking_question", "searching"]);
    expect(result.ok).toBe(true);
  });

  it("counts heartbeats without treating them as content", async () => {
    vi.mocked(fetch).mockResolvedValue(
      sseResponse(": heartbeat\n\n: heartbeat\n\n" + frame("envelope", envelope("answer"))),
    );

    let beats = 0;
    const result = await askTurn({
      query: "q",
      clientRequestId: "r",
      onHeartbeat: () => (beats += 1),
    });

    expect(beats).toBe(2);
    expect(result.ok).toBe(true);
  });

  it("sends the query and the idempotency key, and asks for a stream", async () => {
    vi.mocked(fetch).mockResolvedValue(sseResponse(frame("envelope", envelope("answer"))));
    await askTurn({ query: "my question", clientRequestId: "abc-123" });

    const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit];
    // Same-origin proxy. The browser never learns the backend's address.
    expect(url).toBe("/api/proxy/v1/turns/stream");
    expect(JSON.parse(String(init.body))).toEqual({
      query: "my question",
      client_request_id: "abc-123",
    });
    expect(new Headers(init.headers).get("accept")).toBe("text/event-stream");
  });
});

describe("failure envelopes are outcomes, not faults", () => {
  it.each([
    "failure_generation_unavailable",
    "failure_retrieval_failed",
    "failure_internal_error",
  ])("%s resolves successfully so the patient sees it", async (state) => {
    const failure = envelope(state);
    vi.mocked(fetch).mockResolvedValue(sseResponse(frame("envelope", failure)));

    const result = await ask();
    // ok: true — the request worked. The envelope reports what happened.
    expect(result).toEqual({ ok: true, envelope: failure });
  });
});

describe("transport faults", () => {
  it("reports a rejected fetch as a network failure", async () => {
    vi.mocked(fetch).mockRejectedValue(new TypeError("Failed to fetch"));
    expect(await ask()).toEqual({ ok: false, failure: "network" });
  });

  it("maps 401 to an expired session", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ code: "unauthorized" }), { status: 401 }),
    );
    expect(await ask()).toEqual({ ok: false, failure: "unauthorized" });
  });

  it("distinguishes the two different 409s by their typed code", async () => {
    // Both are 409; they need different copy, so the code decides.
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ code: "session_busy" }), { status: 409 }),
    );
    expect(await ask()).toEqual({ ok: false, failure: "busy" });

    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ code: "turn_limit" }), { status: 409 }),
    );
    expect(await ask()).toEqual({ ok: false, failure: "turn_limit" });
  });

  it("maps 503 to unavailable", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ code: "not_ready" }), { status: 503 }),
    );
    expect(await ask()).toEqual({ ok: false, failure: "unavailable" });
  });

  it("survives an error response with no body at all", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(null, { status: 502 }));
    expect(await ask()).toEqual({ ok: false, failure: "unavailable" });
  });

  it("reports a stream that ends with no envelope", async () => {
    // Stages then silence: the client cannot tell "working" from "gave up", so
    // it must not present this as anything but a fault.
    vi.mocked(fetch).mockResolvedValue(
      sseResponse(frame("stage", { stage: "searching", message: "Searching..." })),
    );
    expect(await ask()).toEqual({ ok: false, failure: "malformed" });
  });

  it("ignores an envelope whose tag it does not recognise", async () => {
    // A newer server sending a fifth variant. Rendering nothing is correct;
    // guessing at a shape this build has no branch for is not.
    vi.mocked(fetch).mockResolvedValue(
      sseResponse(frame("envelope", { kind: "something_new", message: "hi" })),
    );
    expect(await ask()).toEqual({ ok: false, failure: "malformed" });
  });

  it("ignores unparseable JSON in an envelope frame", async () => {
    vi.mocked(fetch).mockResolvedValue(sseResponse("event: envelope\ndata: {not json\n\n"));
    expect(await ask()).toEqual({ ok: false, failure: "malformed" });
  });

  it("keeps an envelope that arrived before the connection dropped", async () => {
    // The work finished; only the socket did not. Discarding a completed answer
    // because the stream ended untidily would waste a real turn.
    const answer = envelope("answer");
    // Pull-based, so the envelope is genuinely DELIVERED before the error. An
    // enqueue immediately followed by error() would discard the queued chunk —
    // that is how ReadableStream is specified, and it is not how a socket that
    // delivered bytes and then dropped behaves.
    let delivered = false;
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        if (!delivered) {
          delivered = true;
          controller.enqueue(new TextEncoder().encode(frame("envelope", answer)));
          return;
        }
        controller.error(new Error("connection reset"));
      },
    });
    vi.mocked(fetch).mockResolvedValue(
      new Response(stream, { headers: { "content-type": "text/event-stream" } }),
    );

    expect(await ask()).toEqual({ ok: true, envelope: answer });
  });

  it("reports a drop that happened before any envelope", async () => {
    let sent = false;
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        if (!sent) {
          sent = true;
          controller.enqueue(
            new TextEncoder().encode(frame("stage", { stage: "searching", message: "..." })),
          );
          return;
        }
        controller.error(new Error("connection reset"));
      },
    });
    vi.mocked(fetch).mockResolvedValue(
      new Response(stream, { headers: { "content-type": "text/event-stream" } }),
    );

    expect(await ask()).toEqual({ ok: false, failure: "network" });
  });

  it("never surfaces a server-supplied message string", async () => {
    // Copy for every one of these lives in the application. Rendering a string
    // from a response body is the habit that eventually renders an exception.
    vi.mocked(fetch).mockResolvedValue(
      new Response(
        JSON.stringify({ code: "unauthorized", message: "Traceback: secret at /srv/app.py" }),
        { status: 401 },
      ),
    );
    const result = await ask();
    expect(JSON.stringify(result)).not.toContain("Traceback");
    expect(JSON.stringify(result)).not.toContain("/srv/app.py");
  });
});

describe("request ids", () => {
  it("produces a different id each time", () => {
    expect(newRequestId()).not.toBe(newRequestId());
  });

  it("still produces one where crypto.randomUUID is unavailable", () => {
    // Insecure origins and older browsers have no randomUUID. The id only has
    // to not collide within one session; it is not a secret.
    vi.stubGlobal("crypto", {});
    expect(newRequestId()).toMatch(/^req-/);
  });
});
