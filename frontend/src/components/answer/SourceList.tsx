import type { Source } from "@/lib/envelope";
import { EXTERNAL_LINK_REL, displayHost, safeExternalUrl } from "@/lib/url";

/**
 * The numbered sources an answer drew on.
 *
 * Three things this component is careful about.
 *
 * **The number is the contract.** It comes from the server's retrieval order
 * and is what every inline marker resolves to. It is never re-derived from
 * array position here — an off-by-one would silently re-attribute a claim to
 * the wrong paper, which is the worst failure this interface has.
 *
 * **Relevance is not quality.** The server sends a 0..1 retrieval score, and
 * the type that carries it says in its own description that it is neither a
 * confidence nor a grade. Printing "91%" beside a citation would be read as
 * "91% reliable" by every patient who saw it, so it is rendered as a coarse
 * band describing *how the search matched*, with the disclaimer attached.
 *
 * **The sources are unreviewed.** Stated here, next to them, not only on the
 * transparency page. A reader who never leaves this screen must still be told.
 */

interface SourceListProps {
  sources: readonly Source[];
  /** Ties each `id` to one turn, so several answers on a page do not collide. */
  turnKey: string;
}

/** How closely retrieval matched, in words. Never a percentage. */
function matchBand(relevance: number | null | undefined): string | null {
  if (relevance === null || relevance === undefined) return null;
  if (relevance >= 0.8) return "Closely matched the question";
  if (relevance >= 0.5) return "Partly matched the question";
  return "Loosely matched the question";
}

export function SourceList({ sources, turnKey }: SourceListProps) {
  if (sources.length === 0) return null;

  return (
    <section aria-labelledby={`${turnKey}-sources`} className="mt-8">
      <h3
        id={`${turnKey}-sources`}
        className="text-[11px] font-semibold uppercase tracking-[0.18em] text-ink-secondary"
      >
        Sources
      </h3>

      <ol className="mt-4 space-y-4">
        {sources.map((source) => {
          const href = safeExternalUrl(source.url);
          const band = matchBand(source.relevance);
          return (
            <li
              key={source.source_id}
              id={`${turnKey}-source-${source.number}`}
              className="flex gap-3 scroll-mt-20"
            >
              <span
                aria-hidden="true"
                className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-rule text-[12px] font-medium text-ink-secondary"
              >
                {source.number}
              </span>
              <div className="min-w-0">
                {/* The number is repeated for a screen reader, which does not
                    see the decorative badge beside it. */}
                <span className="visually-hidden">{`Source ${source.number}. `}</span>
                {href ? (
                  <a
                    href={href}
                    target="_blank"
                    rel={EXTERNAL_LINK_REL}
                    className="text-[15px] leading-snug text-accent underline-offset-4 hover:underline"
                  >
                    {source.title}
                    <span className="visually-hidden"> (opens in a new tab)</span>
                  </a>
                ) : (
                  // No usable link: the title still shows, as text. A source
                  // whose URL did not survive validation is still a citation.
                  <span className="text-[15px] leading-snug text-ink">{source.title}</span>
                )}
                <p className="mt-1 text-[12px] text-ink-secondary">
                  {href ? displayHost(href) : "No link recorded"}
                  {band ? <span aria-hidden="true"> · </span> : null}
                  {band}
                </p>
              </div>
            </li>
          );
        })}
      </ol>

      <p className="mt-5 border-t border-rule pt-4 text-[12px] leading-relaxed text-ink-secondary">
        Match describes how closely the search matched your question. It is not a
        measure of quality, reliability or how much the finding applies to you.
        No clinician has reviewed these sources, and they must not be treated as
        approved or clinically validated.
      </p>
    </section>
  );
}
