import "server-only";

/**
 * Server-side configuration, read once and validated loudly.
 *
 * Every value here is a secret or an internal address. `server-only` makes an
 * accidental client import a build error rather than a bundle that ships a
 * service token to the browser — which is the single worst thing this
 * application could do, because the token is what makes the backend private.
 *
 * There are no development fallbacks. A missing secret refuses to start: a
 * default passcode is an open door with a changelog entry.
 */

/** Where the FastAPI backend lives. Never reachable from the browser. */
export const API_URL = process.env.SYNAPSE_API_URL ?? "http://127.0.0.1:8000";

/** Proves a request came from this server rather than from a page. */
export const SERVICE_TOKEN = process.env.SYNAPSE_SERVICE_TOKEN ?? "";

/**
 * Verifies the access cookie the backend signed. Shared with FastAPI.
 *
 * `DEMO_SESSION_SECRET` is the deployment alias — the name set in Vercel and in
 * Render, where the same value has to appear on both sides or every session
 * silently reads as expired. The `SYNAPSE_*` name stays primary so local
 * development and the test suite are unaffected.
 *
 * Neither name is `NEXT_PUBLIC_`, and this module is `server-only`, so an
 * accidental client import is a build error rather than a bundle that ships the
 * signing secret to the browser.
 */
export const JWT_SECRET =
  process.env.SYNAPSE_JWT_SECRET || process.env.DEMO_SESSION_SECRET || "";

/** How long the backend's access token lasts. Mirrors ACCESS_TOKEN_TTL_SECONDS. */
export const ACCESS_TTL_SECONDS = 8 * 60 * 60;

export const IS_PRODUCTION = process.env.NODE_ENV === "production";

/**
 * The names of everything that must be set, so a failure can say which.
 * Values are never included in a message: an operator who set the wrong
 * variable has put a secret somewhere it does not belong, and echoing it into
 * a log would put it somewhere worse.
 */
export function missingConfiguration(): string[] {
  const missing: string[] = [];
  if (!SERVICE_TOKEN) missing.push("SYNAPSE_SERVICE_TOKEN");
  // Both accepted names are reported: telling an operator who set
  // DEMO_SESSION_SECRET to go and set SYNAPSE_JWT_SECRET sends them to fix
  // something that was never wrong.
  if (!JWT_SECRET) missing.push("SYNAPSE_JWT_SECRET|DEMO_SESSION_SECRET");
  if (!API_URL) missing.push("SYNAPSE_API_URL");
  return missing;
}

export function isConfigured(): boolean {
  return missingConfiguration().length === 0;
}
