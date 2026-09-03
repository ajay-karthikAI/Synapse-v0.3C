import { describe, expect, it } from "vitest";

import { SseParser, readSseStream } from "@/lib/sse";

/**
 * The framing, tested against how a network actually delivers bytes.
 *
 * Every one of these cases is a real failure mode rather than a hypothetical: a
 * chunk boundary in the middle of an event is the normal case on a slow
 * connection, and a parser that assumed one chunk equals one event works
 * perfectly on localhost and drops answers in production.
 */

function frames(parser: SseParser, ...chunks: string[]) {
  return chunks.flatMap((chunk) => parser.push(chunk));
}

describe("framing", () => {
  it("reads one complete event", () => {
    const parser = new SseParser();
    expect(frames(parser, 'event: stage\ndata: {"stage":"searching"}\n\n')).toEqual([
      { type: "event", event: "stage", data: '{"stage":"searching"}' },
    ]);
  });

  it("reads several events delivered in one chunk", () => {
    const parser = new SseParser();
    const result = frames(
      parser,
      "event: stage\ndata: one\n\nevent: stage\ndata: two\n\nevent: envelope\ndata: three\n\n",
    );
    expect(result.map((frame) => frame.type === "event" && frame.data)).toEqual([
      "one",
      "two",
      "three",
    ]);
  });

  it("holds an event split across chunk boundaries until it is complete", () => {
    const parser = new SseParser();
    // Split mid-field, mid-value and mid-terminator: all three happen.
    expect(parser.push("event: enve")).toEqual([]);
    expect(parser.push("lope\ndata: {\"kind\":")).toEqual([]);
    expect(parser.push('"answer"}\n')).toEqual([]);
    expect(parser.push("\n")).toEqual([
      { type: "event", event: "envelope", data: '{"kind":"answer"}' },
    ]);
  });

  it("splits a multi-byte character across chunks without corrupting it", async () => {
    // "…" is three bytes in UTF-8. A decoder without `stream: true` turns a
    // split one into replacement characters, silently mangling the answer.
    const encoder = new TextEncoder();
    const payload = encoder.encode('event: envelope\ndata: {"m":"…"}\n\n');
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(payload.slice(0, 24));
        controller.enqueue(payload.slice(24));
        controller.close();
      },
    });

    const seen: string[] = [];
    await readSseStream(stream, (frame) => {
      if (frame.type === "event") seen.push(frame.data);
    });
    expect(seen).toEqual(['{"m":"…"}']);
    expect(seen[0]).not.toContain("�");
  });

  it("treats CRLF line endings as line endings", () => {
    const parser = new SseParser();
    expect(frames(parser, "event: stage\r\ndata: x\r\n\r\n")).toEqual([
      { type: "event", event: "stage", data: "x" },
    ]);
  });

  it("joins repeated data lines with newlines, per the specification", () => {
    const parser = new SseParser();
    expect(frames(parser, "event: envelope\ndata: a\ndata: b\n\n")).toEqual([
      { type: "event", event: "envelope", data: "a\nb" },
    ]);
  });

  it("strips exactly one leading space after the colon, not all of it", () => {
    const parser = new SseParser();
    // Indented JSON would lose its structure if the parser trimmed greedily.
    expect(frames(parser, "event: envelope\ndata:   spaced\n\n")).toEqual([
      { type: "event", event: "envelope", data: "  spaced" },
    ]);
  });
});

describe("heartbeats", () => {
  it("reports the keep-alive comment rather than discarding it", () => {
    // It carries no data, but it is the only evidence a long turn is alive.
    const parser = new SseParser();
    expect(frames(parser, ": heartbeat\n\n")).toEqual([{ type: "heartbeat" }]);
  });

  it("reads heartbeats interleaved with real events", () => {
    const parser = new SseParser();
    const result = frames(
      parser,
      "event: stage\ndata: one\n\n: heartbeat\n\n: heartbeat\n\nevent: envelope\ndata: done\n\n",
    );
    expect(result.map((frame) => frame.type)).toEqual([
      "event",
      "heartbeat",
      "heartbeat",
      "event",
    ]);
  });
});

describe("malformed input", () => {
  it("discards a trailing partial block rather than parsing half an event", () => {
    // Half an envelope is not an envelope. The caller reports a dropped
    // connection instead, which is recoverable; a truncated JSON parse is not.
    const parser = new SseParser();
    parser.push("event: envelope\ndata: {\"kind\":\"ans");
    expect(parser.flush()).toEqual([
      { type: "event", event: "envelope", data: '{"kind":"ans' },
    ]);
  });

  it("ignores blank blocks", () => {
    const parser = new SseParser();
    expect(frames(parser, "\n\n\n\n")).toEqual([]);
  });

  it("ignores fields it does not understand", () => {
    const parser = new SseParser();
    expect(frames(parser, "id: 7\nretry: 3000\nevent: stage\ndata: x\n\n")).toEqual([
      { type: "event", event: "stage", data: "x" },
    ]);
  });
});

describe("stream reading", () => {
  it("releases the reader even when the handler throws", async () => {
    // An unreleased reader holds the connection open. On a page that can ask
    // several questions that is one leaked socket per turn.
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode("event: stage\ndata: x\n\n"));
        controller.close();
      },
    });

    await expect(
      readSseStream(stream, () => {
        throw new Error("handler exploded");
      }),
    ).rejects.toThrow("handler exploded");

    // Locked would mean the reader was never released.
    expect(stream.locked).toBe(false);
  });
});
