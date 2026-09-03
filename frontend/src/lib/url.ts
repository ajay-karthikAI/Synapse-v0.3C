/**
 * Whether a source's recorded link is safe to render as a link.
 *
 * Source URLs come from the corpus, which comes from ingestion, which comes
 * from the internet. React escapes text, so a hostile string cannot inject
 * markup — but `href` is not text. `javascript:alert(1)` in an `href` executes
 * on click, and React does not stop it (it warns, in development only, and
 * renders the attribute anyway).
 *
 * So the scheme is checked against an allow-list of exactly two, parsed rather
 * than pattern-matched. A regular expression over the raw string is defeated by
 * `java\nscript:`, by tab characters, by percent-encoding and by leading
 * control characters, all of which `new URL()` resolves before reporting the
 * protocol.
 *
 * The backend already blanks non-http(s) URLs when it builds a `SourceModel`.
 * This is the second check, in the layer that actually writes the attribute,
 * because "the other side validated it" is the assumption every injection bug
 * is built on.
 */

/** The only two schemes that may reach an `href`. */
const SAFE_PROTOCOLS = new Set(["http:", "https:"]);

/**
 * The URL if it is safe to link to, otherwise `null`.
 *
 * A `null` return means "render the title as text, with no link" — never
 * "render it anyway". Callers are typed to force that choice.
 */
export function safeExternalUrl(raw: string | undefined | null): string | null {
  if (!raw) return null;
  const trimmed = raw.trim();
  if (trimmed.length === 0) return null;

  let parsed: URL;
  try {
    // No base: a relative URL has no scheme to trust, and a source that
    // recorded one is malformed rather than local.
    parsed = new URL(trimmed);
  } catch {
    return null;
  }

  if (!SAFE_PROTOCOLS.has(parsed.protocol)) return null;
  // Re-serialised from the parse rather than returning the input, so whatever
  // encoding tricks survived the constructor are normalised away.
  return parsed.toString();
}

/**
 * The attributes every external link on this site carries.
 *
 * `noopener` denies the opened page a handle to this one through
 * `window.opener` — without it, a source can navigate the tab it came from to a
 * page that looks like this one and asks for the passcode. `noreferrer`
 * withholds the referrer, which would otherwise tell a third party that a
 * particular Synapse page linked out, and on a URL carrying a query would tell
 * them what was asked. `nofollow` because these are retrieved citations, not
 * endorsements, and this deployment vouches for none of them.
 */
export const EXTERNAL_LINK_REL = "noopener noreferrer nofollow";

/** The host, for display beside a link. Never used as an `href`. */
export function displayHost(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}
