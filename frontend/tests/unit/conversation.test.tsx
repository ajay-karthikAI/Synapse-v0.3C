import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Conversation } from "@/components/chat/Conversation";

import { envelope } from "../fixtures/load";

/**
 * The conversation, end to end against a mocked network.
 *
 * These are integration tests: a real composer, a real transport, a real
 * envelope from the golden fixtures, and a `fetch` that answers like the proxy
 * does. What they assert is behaviour a component test cannot reach — that the
 * draft survives a failure, that a second submission cannot be started, and
 * that clearing removes the conversation from both the page and the server.
 */

const EXAMPLES = ["What does my HbA1c number actually mean?"] as const;

function sse(body: string): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(body));
      controller.close();
    },
  });
  return new Response(stream, { headers: { "content-type": "text/event-stream" } });
}

const frame = (event: string, payload: unknown) =>
  `event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`;

/** Routes a mocked `fetch` by path, so one test can answer several endpoints. */
function route(handlers: Record<string, () => Response>) {
  return vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    const key = Object.keys(handlers).find((pattern) => {
      const [patternMethod, patternPath] = pattern.split(" ");
      return method === patternMethod && url.includes(String(patternPath));
    });
    if (!key) return Promise.resolve(new Response("{}", { status: 404 }));
    return Promise.resolve(handlers[key]!());
  });
}

const SESSION_EMPTY = () =>
  new Response(
    JSON.stringify({
      session_id: "s1",
      turn_count: 0,
      max_turns: 20,
      turn_active: false,
      idle_ttl_seconds: 7200,
    }),
    { headers: { "content-type": "application/json" } },
  );

beforeEach(() => {
  vi.stubGlobal("fetch", route({ "GET v1/session": SESSION_EMPTY }));
});
afterEach(() => {
  vi.unstubAllGlobals();
});

async function ask(user: ReturnType<typeof userEvent.setup>, text: string) {
  const field = screen.getByLabelText(/your question or symptoms/i);
  await user.type(field, text);
  await user.click(screen.getByRole("button", { name: /^ask$/i }));
}

describe("asking a question", () => {
  it("renders the answer and clears the draft", async () => {
    vi.stubGlobal(
      "fetch",
      route({
        "GET v1/session": SESSION_EMPTY,
        "POST v1/turns/stream": () => sse(frame("envelope", envelope("answer"))),
      }),
    );
    const user = userEvent.setup();
    render(<Conversation examples={EXAMPLES} />);

    await ask(user, "what does my HbA1c mean?");

    expect(await screen.findByRole("heading", { name: /what the research says/i })).toBeInTheDocument();
    // Cleared only because an envelope actually arrived.
    expect(screen.getByLabelText(/your question or symptoms/i)).toHaveValue("");
    // The patient's own words are echoed above the result.
    expect(screen.getByText("what does my HbA1c mean?")).toBeInTheDocument();
  });

  it("fills the field from an example chip", async () => {
    const user = userEvent.setup();
    render(<Conversation examples={EXAMPLES} />);

    await user.click(screen.getByRole("button", { name: EXAMPLES[0] }));
    expect(screen.getByLabelText(/your question or symptoms/i)).toHaveValue(EXAMPLES[0]);
  });

  it("shows the permanent identifier warning before anything is asked", () => {
    render(<Conversation examples={EXAMPLES} />);
    expect(screen.getByText(/do not enter anything that identifies you/i)).toBeInTheDocument();
    // No dismiss control: it stays for as long as the field it applies to.
    expect(screen.queryByRole("button", { name: /dismiss|close|got it/i })).toBeNull();
  });

  it("keeps the warning visible after an answer has been rendered", async () => {
    vi.stubGlobal(
      "fetch",
      route({
        "GET v1/session": SESSION_EMPTY,
        "POST v1/turns/stream": () => sse(frame("envelope", envelope("answer"))),
      }),
    );
    const user = userEvent.setup();
    render(<Conversation examples={EXAMPLES} />);
    await ask(user, "q");
    await screen.findByRole("heading", { name: /what the research says/i });

    expect(screen.getByText(/do not enter anything that identifies you/i)).toBeInTheDocument();
  });
});

describe("duplicate submission is impossible", () => {
  it("disables the field and the button while a turn runs", async () => {
    let release!: () => void;
    const held = new Promise<void>((resolve) => (release = resolve));
    vi.stubGlobal(
      "fetch",
      route({
        "GET v1/session": SESSION_EMPTY,
        "POST v1/turns/stream": () => {
          const stream = new ReadableStream<Uint8Array>({
            async pull(controller) {
              await held;
              controller.enqueue(
                new TextEncoder().encode(frame("envelope", envelope("answer"))),
              );
              controller.close();
            },
          });
          return new Response(stream, { headers: { "content-type": "text/event-stream" } });
        },
      }),
    );
    const user = userEvent.setup();
    render(<Conversation examples={EXAMPLES} />);

    await ask(user, "a slow question");

    const button = await screen.findByRole("button", { name: /working/i });
    expect(button).toBeDisabled();
    expect(screen.getByLabelText(/your question or symptoms/i)).toBeDisabled();

    release();
    await screen.findByRole("heading", { name: /what the research says/i });
  });

  it("issues exactly one request per submission", async () => {
    const fetchMock = route({
      "GET v1/session": SESSION_EMPTY,
      "POST v1/turns/stream": () => sse(frame("envelope", envelope("answer"))),
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<Conversation examples={EXAMPLES} />);

    await ask(user, "one question");
    await screen.findByRole("heading", { name: /what the research says/i });

    const turnCalls = fetchMock.mock.calls.filter(([url]) => String(url).includes("turns/stream"));
    expect(turnCalls).toHaveLength(1);
  });

  it("does not submit an empty or whitespace-only question", async () => {
    const fetchMock = route({ "GET v1/session": SESSION_EMPTY });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<Conversation examples={EXAMPLES} />);

    expect(screen.getByRole("button", { name: /^ask$/i })).toBeDisabled();
    await user.type(screen.getByLabelText(/your question or symptoms/i), "    ");
    expect(screen.getByRole("button", { name: /^ask$/i })).toBeDisabled();
  });
});

describe("error recovery preserves the draft", () => {
  it.each([
    ["a dropped connection", () => Promise.reject(new TypeError("Failed to fetch"))],
    ["an unavailable backend", () => new Response("{}", { status: 503 })],
  ])("keeps the question in the box after %s", async (_name, respond) => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) =>
        String(input).includes("turns/stream")
          ? Promise.resolve(respond() as Response).catch((error: unknown) =>
              Promise.reject(error),
            )
          : Promise.resolve(SESSION_EMPTY()),
      ),
    );
    const user = userEvent.setup();
    render(<Conversation examples={EXAMPLES} />);

    const question = "a carefully worded description of a symptom";
    await ask(user, question);

    // The whole point: the text a patient took time over is still there.
    await waitFor(() =>
      expect(screen.getByLabelText(/your question or symptoms/i)).toHaveValue(question),
    );
    expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument();
  });

  it("retries under the SAME request id, so the server replays rather than re-runs", async () => {
    let attempt = 0;
    // Bodies are recorded here rather than read back off `mock.calls`, which
    // types them from the mock's own signature and needs a parameter this
    // implementation has no use for.
    const sent: { query: string; client_request_id: string }[] = [];
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (!String(input).includes("turns/stream")) return Promise.resolve(SESSION_EMPTY());
      sent.push(JSON.parse(String(init?.body)));
      attempt += 1;
      if (attempt === 1) return Promise.reject(new TypeError("Failed to fetch"));
      return Promise.resolve(sse(frame("envelope", envelope("answer"))));
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<Conversation examples={EXAMPLES} />);

    await ask(user, "why is my blood sugar high?");
    await screen.findByRole("button", { name: /try again/i });
    await user.click(screen.getByRole("button", { name: /try again/i }));
    await screen.findByRole("heading", { name: /what the research says/i });

    expect(sent).toHaveLength(2);
    const [first, second] = sent as [(typeof sent)[number], (typeof sent)[number]];
    // Identical id: a turn that already ran is replayed, not charged twice
    // against the session's question ceiling.
    expect(first.client_request_id).toBe(second.client_request_id);
    expect(first.query).toBe(second.query);
  });

  it("offers sign-in rather than a retry when the session has expired", async () => {
    vi.stubGlobal(
      "fetch",
      route({
        "GET v1/session": SESSION_EMPTY,
        "POST v1/turns/stream": () =>
          new Response(JSON.stringify({ code: "unauthorized" }), { status: 401 }),
      }),
    );
    const user = userEvent.setup();
    render(<Conversation examples={EXAMPLES} />);
    await ask(user, "q");

    expect(await screen.findByRole("link", { name: /sign in again/i })).toHaveAttribute(
      "href",
      "/access",
    );
    expect(screen.queryByRole("button", { name: /try again/i })).toBeNull();
  });

  it("says something different for each transport cause", async () => {
    vi.stubGlobal(
      "fetch",
      route({
        "GET v1/session": SESSION_EMPTY,
        "POST v1/turns/stream": () =>
          new Response(JSON.stringify({ code: "turn_limit" }), { status: 409 }),
      }),
    );
    const user = userEvent.setup();
    render(<Conversation examples={EXAMPLES} />);
    await ask(user, "q");

    expect(await screen.findByText(/reached its limit of questions/i)).toBeInTheDocument();
  });
});

describe("session restoration", () => {
  it("reports turns the server still holds, and says why they cannot be shown", async () => {
    vi.stubGlobal(
      "fetch",
      route({
        "GET v1/session": () =>
          new Response(
            JSON.stringify({
              session_id: "s1",
              turn_count: 3,
              max_turns: 20,
              turn_active: false,
              idle_ttl_seconds: 7200,
            }),
            { headers: { "content-type": "application/json" } },
          ),
      }),
    );
    render(<Conversation examples={EXAMPLES} />);

    // Honest rather than silent: the API exposes counts, never content, so a
    // conversation genuinely cannot be replayed.
    expect(await screen.findByText(/already has 3 questions/i)).toBeInTheDocument();
    expect(screen.getByText(/never stores what was asked or answered/i)).toBeInTheDocument();
  });

  it("says nothing when the session is empty", async () => {
    render(<Conversation examples={EXAMPLES} />);
    await waitFor(() => expect(screen.queryByText(/already has/i)).toBeNull());
  });
});

describe("clearing the conversation", () => {
  it("removes the turns from the page and deletes the session on the server", async () => {
    const fetchMock = route({
      "GET v1/session": SESSION_EMPTY,
      "POST v1/turns/stream": () => sse(frame("envelope", envelope("answer"))),
      "DELETE v1/session": () =>
        new Response(JSON.stringify({ deleted: true }), {
          headers: { "content-type": "application/json" },
        }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<Conversation examples={EXAMPLES} />);

    await ask(user, "q");
    await screen.findByRole("heading", { name: /what the research says/i });

    await user.click(screen.getByRole("button", { name: /clear this conversation/i }));

    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: /what the research says/i })).toBeNull(),
    );
    // Both halves: either alone leaves the other holding the conversation.
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) =>
          String(url).includes("v1/session") && (init as RequestInit)?.method === "DELETE",
      ),
    ).toBe(true);
  });
});

describe("the appointment brief is offered only where one exists", () => {
  it.each([
    ["answer", true],
    ["emergency", false],
    ["failure_evidence_unavailable", false],
    ["failure_internal_error", false],
  ])("%s offers a brief: %s", async (state, offered) => {
    vi.stubGlobal(
      "fetch",
      route({
        "GET v1/session": SESSION_EMPTY,
        "POST v1/turns/stream": () => sse(frame("envelope", envelope(state))),
      }),
    );
    const user = userEvent.setup();
    render(<Conversation examples={EXAMPLES} />);
    await ask(user, "q");

    await waitFor(() =>
      expect(screen.getAllByRole("heading", { level: 2 }).length).toBeGreaterThan(1),
    );
    const button = screen.queryByRole("button", { name: /build an appointment brief/i });
    // Absent, not disabled: there is nothing to build one from on those paths.
    expect(button === null).toBe(!offered);
  });
});
