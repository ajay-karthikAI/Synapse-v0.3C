/**
 * A server-sent-events reader, written as a pure function over chunks.
 *
 * `EventSource` is not usable here for two reasons: it cannot issue a POST, and
 * it cannot carry the request body a turn needs. So the stream is read from
 * `fetch` and framed by hand, which means the framing has to be correct rather
 * than assumed.
 *
 * Three details that are easy to get wrong, and are tested directly:
 *
 * **Chunks are not events.** A network chunk can split an event in half, or
 * carry three at once. The parser holds a buffer and only emits on a blank
 * line, so an event is never dispatched half-read.
 *
 * **Heartbeats are comments.** The backend writes `: heartbeat` to keep proxies
 * from closing an idle connection during a long turn. A line beginning with `:`
 * is a comment by the specification and carries no data — but it is also the
 * evidence that the connection is alive, so it is surfaced rather than dropped.
 *
 * **`data:` may repeat.** Multiple `data:` lines in one event are joined with
 * newlines. The backend emits compact single-line JSON so this should not
 * arise, but a parser that assumed it never would is a parser that mangles the
 * payload on the day it does.
 */

/** One framed event, or the keep-alive comment that carries no event. */
export type SseFrame =
  | { type: "event"; event: string; data: string }
  | { type: "heartbeat" };

/**
 * Incremental SSE framing.
 *
 * Feed it chunks; it returns whatever complete frames those chunks completed.
 * State is the unterminated tail, held until the rest of it arrives.
 */
export class SseParser {
  private buffer = "";

  /** Frames completed by this chunk. Possibly none; possibly several. */
  push(chunk: string): SseFrame[] {
    // Normalise line endings first: CRLF is legal in the wire format, and
    // splitting on "\n\n" alone would never match a CRLF-framed stream.
    this.buffer += chunk.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
    const frames: SseFrame[] = [];

    let boundary = this.buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const block = this.buffer.slice(0, boundary);
      this.buffer = this.buffer.slice(boundary + 2);
      const frame = parseBlock(block);
      if (frame) frames.push(frame);
      boundary = this.buffer.indexOf("\n\n");
    }
    return frames;
  }

  /**
   * Whatever a final unterminated block contained.
   *
   * A well-behaved stream ends on a blank line and this returns nothing. A
   * stream cut off mid-event leaves a partial block, which is discarded rather
   * than parsed: half an envelope is not an envelope.
   */
  flush(): SseFrame[] {
    const tail = this.buffer;
    this.buffer = "";
    if (!tail.includes("\n") && !tail.startsWith(":")) return [];
    const frame = parseBlock(tail);
    return frame ? [frame] : [];
  }
}

function parseBlock(block: string): SseFrame | null {
  if (block.trim().length === 0) return null;

  let event = "";
  const data: string[] = [];
  let sawComment = false;

  for (const line of block.split("\n")) {
    if (line.startsWith(":")) {
      sawComment = true;
      continue;
    }
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    // One optional leading space after the colon is part of the framing, not
    // of the value. Stripping more would corrupt indented payloads.
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);

    if (field === "event") event = value;
    else if (field === "data") data.push(value);
  }

  if (event === "" && data.length === 0) {
    return sawComment ? { type: "heartbeat" } : null;
  }
  return { type: "event", event, data: data.join("\n") };
}

/**
 * Read a `fetch` body to completion, dispatching each frame as it arrives.
 *
 * The reader is always released, including when `onFrame` throws — an unreleased
 * reader holds the connection open, and on a page that can ask several questions
 * that leaks one socket per turn.
 */
export async function readSseStream(
  body: ReadableStream<Uint8Array>,
  onFrame: (frame: SseFrame) => void,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  const parser = new SseParser();

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      // `stream: true` so a multi-byte character split across two chunks is
      // held rather than decoded into a replacement character.
      for (const frame of parser.push(decoder.decode(value, { stream: true }))) {
        onFrame(frame);
      }
    }
    for (const frame of parser.flush()) onFrame(frame);
  } finally {
    reader.releaseLock();
  }
}
