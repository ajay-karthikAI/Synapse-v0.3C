import { isTurnEnvelope, type TurnEnvelope } from "@/lib/envelope";
import { readSseStream } from "@/lib/sse";

/**
 * Asking one question, over the proxy.
 *
 * The browser never talks to FastAPI. Every call here goes to
 * `/api/proxy/...` on this same origin, which attaches the service token and
 * the access cookie server-side (`src/app/api/proxy/[...path]/route.ts`). That
 * is why nothing in this file has a base URL, a token, or a credential.
 *
 * The shape of a turn is: zero or more `stage` events, then **exactly one**
 * `envelope` event. The server guarantees the envelope on every path — success,
 * failure, exception and deadline alike — so a stream that ends without one is
 * a transport fault, not an outcome, and is reported as such. Distinguishing
 * those two is the whole reason `askTurn` resolves with a tagged result rather
 * than throwing: a failure envelope is something a patient should read, and a
 * dropped connection is something they should be able to retry.
 */

/** A progress stage. Both fields come from the server's frozen message table. */
export interface StageEvent {
  stage: string;
  message: string;
}

/** Why a turn did not produce an envelope. All are recoverable by retrying. */
export type TransportFailure =
  | "network" // The request never completed
  | "unauthorized" // The session expired or the cookie was cleared
  | "busy" // A turn is already running on this session
  | "turn_limit" // The session's question ceiling
  | "unavailable" // The backend is not ready, or not reachable
  | "malformed"; // A response arrived that this client cannot parse

export type TurnResult =
  | { ok: true; envelope: TurnEnvelope }
  | { ok: false; failure: TransportFailure };

export interface AskOptions {
  query: string;
  /** Makes a retry idempotent: the same id replays rather than re-running. */
  clientRequestId: string;
  onStage?: (event: StageEvent) => void;
  /** Called for each keep-alive comment: evidence the turn is still running. */
  onHeartbeat?: () => void;
  signal?: AbortSignal;
}

/** Maps the backend's typed refusal codes onto what the interface can do next. */
const FAILURE_BY_STATUS: Record<number, TransportFailure> = {
  401: "unauthorized",
  403: "unauthorized",
  409: "busy",
  503: "unavailable",
  502: "unavailable",
};

export async function askTurn(options: AskOptions): Promise<TurnResult> {
  let response: Response;
  try {
    response = await fetch("/api/proxy/v1/turns/stream", {
      method: "POST",
      headers: { "content-type": "application/json", accept: "text/event-stream" },
      body: JSON.stringify({
        query: options.query,
        client_request_id: options.clientRequestId,
      }),
      ...(options.signal ? { signal: options.signal } : {}),
    });
  } catch {
    // Includes an aborted request. The caller knows whether it aborted.
    return { ok: false, failure: "network" };
  }

  if (!response.ok) {
    return { ok: false, failure: await classify(response) };
  }
  if (!response.body) {
    return { ok: false, failure: "malformed" };
  }

  let envelope: TurnEnvelope | null = null;
  try {
    await readSseStream(response.body, (frame) => {
      if (frame.type === "heartbeat") {
        options.onHeartbeat?.();
        return;
      }
      if (frame.event === "stage") {
        const stage = parseStage(frame.data);
        if (stage) options.onStage?.(stage);
        return;
      }
      if (frame.event === "envelope") {
        const parsed = parseJson(frame.data);
        // Last one wins rather than first: the server sends exactly one, and
        // preferring the last means a replayed stream cannot pin a stale value.
        if (isTurnEnvelope(parsed)) envelope = parsed;
      }
    });
  } catch {
    // The connection dropped mid-turn. If the envelope had already arrived it
    // is still good — the work finished, only the socket did not.
    return envelope ? { ok: true, envelope } : { ok: false, failure: "network" };
  }

  return envelope ? { ok: true, envelope } : { ok: false, failure: "malformed" };
}

/**
 * Turn an error response into a cause.
 *
 * The body is read for the backend's typed `code`, because 409 covers both "a
 * turn is already running" and "this session is out of questions" and those
 * need different copy. The `message` is deliberately NOT used: application copy
 * for every one of these lives in this repository, and rendering a string from
 * a response body is the habit that eventually renders an exception.
 */
async function classify(response: Response): Promise<TransportFailure> {
  let code = "";
  try {
    const body: unknown = await response.json();
    if (typeof body === "object" && body !== null) {
      const value = (body as { code?: unknown }).code;
      if (typeof value === "string") code = value;
    }
  } catch {
    // No body, or not JSON. The status alone decides.
  }
  if (code === "turn_limit") return "turn_limit";
  if (code === "session_busy") return "busy";
  return FAILURE_BY_STATUS[response.status] ?? "network";
}

function parseStage(data: string): StageEvent | null {
  const parsed = parseJson(data);
  if (typeof parsed !== "object" || parsed === null) return null;
  const { stage, message } = parsed as { stage?: unknown; message?: unknown };
  if (typeof stage !== "string" || typeof message !== "string") return null;
  return { stage, message };
}

function parseJson(data: string): unknown {
  try {
    return JSON.parse(data);
  } catch {
    return null;
  }
}

/**
 * A fresh idempotency key.
 *
 * `crypto.randomUUID` is unavailable on insecure origins and in some older
 * browsers, so there is a fallback. It does not need to be unguessable — the id
 * is scoped to one already-authenticated session and only ever selects that
 * session's own stored result — it needs to not collide.
 */
export function newRequestId(): string {
  const cryptoRef = globalThis.crypto as Crypto | undefined;
  if (cryptoRef?.randomUUID) return cryptoRef.randomUUID();
  return `req-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}
