import type { Excerpt } from "@/lib/envelope";

/**
 * The verbatim passages the verifier matched.
 *
 * This is the component that makes the rest of the answer checkable. Every
 * other block is the model's words about the research; these are the research's
 * own words, quoted exactly, and the claims above were kept only because they
 * matched one of them.
 *
 * They are rendered inside a `<details>`, closed by default. Not to hide them —
 * a closed disclosure is one keystroke from open and is announced as such — but
 * because a wall of abstract text above the questions-for-your-doctor would
 * bury the part a patient actually takes to their appointment. The summary says
 * how many there are, so opening it is an informed choice.
 *
 * `<blockquote>` and `<cite>` are used for what they mean. A screen reader user
 * gets "quote" and the attribution as structure, not as punctuation they have
 * to infer.
 */

interface ExcerptListProps {
  excerpts: readonly Excerpt[];
  turnKey: string;
}

export function ExcerptList({ excerpts, turnKey }: ExcerptListProps) {
  if (excerpts.length === 0) return null;

  return (
    <details className="mt-8 rounded-[14px] border border-rule bg-sunken/60">
      <summary className="cursor-pointer list-none px-4 py-3 text-[13px] font-medium text-accent marker:content-none">
        {excerpts.length === 1
          ? "Show the passage this came from"
          : `Show the ${excerpts.length} passages this came from`}
      </summary>

      <div className="border-t border-rule px-4 pb-4 pt-3">
        <p className="text-[12px] leading-relaxed text-ink-secondary">
          Quoted exactly as published. Each statement above was kept only because
          it matched one of these.
        </p>
        <ul className="mt-4 space-y-4">
          {excerpts.map((excerpt) => (
            <li key={excerpt.chunk_id}>
              <blockquote className="border-l-2 border-accent-soft/60 pl-3.5">
                <p className="text-[14px] leading-relaxed text-ink">{excerpt.quote}</p>
              </blockquote>
              <cite className="mt-1.5 block pl-3.5 text-[12px] not-italic text-ink-secondary">
                <a
                  href={`#${turnKey}-source-${excerpt.source_number}`}
                  className="text-accent no-underline hover:underline"
                >
                  Source {excerpt.source_number}
                </a>
              </cite>
            </li>
          ))}
        </ul>
      </div>
    </details>
  );
}
