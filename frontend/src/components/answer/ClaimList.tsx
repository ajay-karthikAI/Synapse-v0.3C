import type { Claim } from "@/lib/envelope";

/**
 * The claims, each with the sources it rests on.
 *
 * Two rules from the answer layer show up as markup here.
 *
 * **A withheld claim is absent, not greyed out.** The display policy removed it
 * on the server; there is no "hidden" state to render, and no prop through
 * which one could be passed. If a claim is in this array it passed citation
 * verification.
 *
 * **Partial support is shown and labelled.** The server never quietly rewrites
 * an overstated claim into a supported one, so the interface must not quietly
 * present it as one either. It gets a visible badge and a real sentence saying
 * what that means, because "partially supported" is jargon on its own.
 *
 * Every citation marker is a link to the numbered source below it, so following
 * a claim to its evidence is one keyboard action rather than a scroll and a
 * count.
 */

interface ClaimListProps {
  claims: readonly Claim[];
  turnKey: string;
}

export function ClaimList({ claims, turnKey }: ClaimListProps) {
  if (claims.length === 0) return null;

  return (
    <ul className="mt-6 space-y-5">
      {claims.map((claim) => {
        const partial = claim.support === "partially_supported";
        return (
          <li
            key={claim.claim_id}
            className={`rounded-[14px] border px-4 py-3.5 ${
              partial ? "border-warning/30 bg-warning-surface/40" : "border-rule bg-surface"
            }`}
          >
            <p className="text-[15px] leading-relaxed text-ink">
              {claim.text}{" "}
              <Citations claim={claim} turnKey={turnKey} />
            </p>

            {partial ? (
              <p className="mt-2.5 flex flex-wrap items-baseline gap-x-2 text-[12px] leading-relaxed text-warning">
                <span className="rounded-[6px] border border-warning/40 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider">
                  Partly supported
                </span>
                <span className="text-ink-secondary">
                  The source backs part of this statement but not all of it. Read
                  the quoted passage before relying on it.
                </span>
              </p>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

/** The inline `[1][2]` markers, each linking to its entry in the source list. */
function Citations({ claim, turnKey }: { claim: Claim; turnKey: string }) {
  if (claim.source_numbers.length === 0) return null;

  return (
    <span className="whitespace-nowrap">
      {claim.source_numbers.map((number) => (
        <a
          key={number}
          href={`#${turnKey}-source-${number}`}
          // The visible text is a bare bracketed number, which reads as
          // "bracket one bracket" or is skipped entirely. The label says what
          // it actually is.
          aria-label={`Source ${number} for this statement`}
          className="ml-0.5 align-baseline text-[12px] font-medium text-accent no-underline hover:underline"
        >
          [{number}]
        </a>
      ))}
    </span>
  );
}
