import type { Brief, SessionState } from "@/lib/envelope";

/**
 * The non-streaming calls: session state, and editing a brief.
 *
 * Every one of these goes through the same-origin proxy, and every one returns
 * `null` rather than throwing on failure. That is deliberate. None of these
 * calls is the answer to a medical question; they are the interface's own
 * housekeeping, and a browser that cannot reach its session endpoint should
 * degrade — show the composer, hide the brief — rather than replace the page
 * with an error boundary the patient can do nothing about.
 *
 * **A brief is never sent as a whole.** Each edit is one named operation with
 * one field, matching `synapse/api/routes/brief.py`. The server holds the
 * brief; the client asks it to change one thing and re-renders whatever comes
 * back. A client that could PUT an entire brief could put unverified claims on
 * a document a patient carries to an appointment, so no function here can
 * express that.
 */

const JSON_HEADERS = { "content-type": "application/json" };

async function requestJson<T>(path: string, init?: RequestInit): Promise<T | null> {
  try {
    const response = await fetch(`/api/proxy/${path}`, {
      cache: "no-store",
      ...init,
    });
    if (!response.ok) return null;
    return (await response.json()) as T;
  } catch {
    return null;
  }
}

/** Counts and clocks. Never conversation content — the API exposes none. */
export function readSession(): Promise<SessionState | null> {
  return requestJson<SessionState>("v1/session");
}

/**
 * Discard everything the server holds for this session.
 *
 * The server-side half of "clear conversation". The client clears its own
 * rendered turns separately; both are required, because either alone leaves the
 * other holding the conversation.
 */
export async function deleteSession(): Promise<boolean> {
  const result = await requestJson<{ deleted: boolean }>("v1/session", {
    method: "DELETE",
  });
  return result?.deleted ?? false;
}

// One brief for the whole conversation, so there is no turn index in the path.
// It was `v1/turns/{i}/brief` until a patient asking four questions ended up
// with four documents, none of which was the sheet of paper they needed.
//
// Exported so the proxy's allow-list can be tested against it: the move out of
// `v1/turns` put the brief outside every prefix the proxy permitted, and the
// proxy answered a 404 of its own making rather than calling the backend.
export const BRIEF_PATH = "v1/brief";

export function readBrief(): Promise<Brief | null> {
  return requestJson<Brief>(BRIEF_PATH);
}

export function setBriefTopic(topic: string): Promise<Brief | null> {
  return requestJson<Brief>(`${BRIEF_PATH}/topic`, {
    method: "PUT",
    headers: JSON_HEADERS,
    body: JSON.stringify({ topic }),
  });
}

export function setBriefNotes(notes: string): Promise<Brief | null> {
  return requestJson<Brief>(`${BRIEF_PATH}/notes`, {
    method: "PUT",
    headers: JSON_HEADERS,
    body: JSON.stringify({ notes }),
  });
}

export function addBriefQuestion(text: string): Promise<Brief | null> {
  return requestJson<Brief>(`${BRIEF_PATH}/questions`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ text }),
  });
}

export function removeBriefQuestion(questionId: string): Promise<Brief | null> {
  // Encoded because it lands in the path. Server-generated today, but a client
  // that assumes an identifier is path-safe is one identifier change from a
  // broken request.
  return requestJson<Brief>(
    `${BRIEF_PATH}/questions/${encodeURIComponent(questionId)}`,
    { method: "DELETE" },
  );
}

export function reorderBriefQuestions(order: readonly string[]): Promise<Brief | null> {
  return requestJson<Brief>(`${BRIEF_PATH}/questions/order`, {
    method: "PUT",
    headers: JSON_HEADERS,
    body: JSON.stringify({ order }),
  });
}

export function setBriefSections(sections: readonly string[]): Promise<Brief | null> {
  return requestJson<Brief>(`${BRIEF_PATH}/sections`, {
    method: "PUT",
    headers: JSON_HEADERS,
    body: JSON.stringify({ sections }),
  });
}

/**
 * The export formats the brief route serves.
 *
 * `pdf` is first because it is what the download button asks for: the server
 * renders it, so a patient on a phone taps once and gets a file, instead of
 * hunting for "save as PDF" inside a print preview.
 */
export const EXPORT_FORMATS = ["pdf", "html", "text", "json"] as const;
export type ExportFormat = (typeof EXPORT_FORMATS)[number];

/**
 * Where an export is downloaded from.
 *
 * A plain `href` on a real link, not a scripted fetch-and-blob. The response
 * carries `Content-Disposition: attachment` and `nosniff` from the server, so
 * the browser saves it with the server's filename and content type. Building a
 * blob URL in the page would discard both and put the patient's own notes into
 * a JavaScript string for no gain.
 */
export function exportUrl(format: ExportFormat): string {
  return `/api/proxy/${BRIEF_PATH}/export/${format}`;
}
