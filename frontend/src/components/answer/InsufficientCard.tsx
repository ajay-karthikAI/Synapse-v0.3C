import type { InsufficientDetail, InsufficientEnvelope } from "@/lib/envelope";

/**
 * No verified evidence — a designed outcome, not an error.
 *
 * This is the state the whole system exists to be able to reach. A tool that
 * always answers is a tool that answers when it should not, and the cost of
 * that is a patient acting on something invented. So this screen is written to
 * be a result: it says what happened, says plainly that it is not a judgement
 * about the person, and gives them something to do next.
 *
 * It appears in two places, which is why the body is a separate export:
 * standalone, when retrieval found nothing eligible; and nested inside an
 * answer, when every claim failed verification and was withheld (an abstention).
 * Both render the identical block, because they are the same finding.
 *
 * `reason` is deliberately not shown. It is an operator-facing code — the
 * envelope's own field description says so — and "no_eligible_evidence" on a
 * patient's screen is noise at best. Operators read it from the API.
 */

interface InsufficientCardProps {
  envelope: InsufficientEnvelope;
  headingId: string;
  headingRef: React.Ref<HTMLHeadingElement>;
}

export function InsufficientCard({ envelope, headingId, headingRef }: InsufficientCardProps) {
  return (
    <article aria-labelledby={headingId} className="mt-8">
      <InsufficientBody
        detail={envelope.detail}
        headingLevel={2}
        headingId={headingId}
        headingRef={headingRef}
      />
      <p className="mt-8 border-t border-rule pt-5 text-[13px] leading-relaxed text-ink-secondary">
        {envelope.disclaimer}
      </p>
    </article>
  );
}

interface InsufficientBodyProps {
  detail: InsufficientDetail;
  /** 2 standalone, 3 when nested under an answer's own heading. */
  headingLevel: 2 | 3;
  headingId?: string;
  headingRef?: React.Ref<HTMLHeadingElement>;
}

/**
 * The card itself, at whichever heading level its context requires.
 *
 * The level is a prop rather than fixed because heading order is structure, and
 * an `h2` nested inside a section already introduced by an `h2` is a broken
 * outline for anyone navigating by headings. An E2E test walks the document and
 * fails if a level is ever skipped.
 */
export function InsufficientBody({
  detail,
  headingLevel,
  headingId,
  headingRef,
}: InsufficientBodyProps) {
  const Heading = headingLevel === 2 ? "h2" : "h3";

  return (
    <div className="rounded-[16px] border border-rule bg-surface px-5 py-5">
      <Heading
        {...(headingId ? { id: headingId } : {})}
        {...(headingRef ? { ref: headingRef } : {})}
        tabIndex={-1}
        className={`text-display focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-accent ${
          headingLevel === 2
            ? "font-display text-[1.6rem] leading-snug font-normal"
            : "text-[15px] font-semibold"
        }`}
      >
        {detail.heading}
      </Heading>

      <p className="mt-3 text-[15px] leading-relaxed text-ink">{detail.message}</p>

      {/* The sentence that stops "we found nothing" being read as "there is
          nothing wrong with you". It is emphasised because that misreading is
          the specific harm this state can cause. */}
      <p className="mt-4 rounded-[12px] border border-accent-soft/25 bg-accent-wash px-4 py-3 text-[14px] leading-relaxed text-ink">
        {detail.not_a_judgement}
      </p>

      {detail.next_steps.length > 0 ? (
        <>
          <h4 className="mt-6 text-[11px] font-semibold uppercase tracking-[0.18em] text-ink-secondary">
            What you can do
          </h4>
          <ul className="mt-3 space-y-2.5">
            {detail.next_steps.map((step) => (
              <li key={step} className="flex gap-2.5 text-[15px] leading-relaxed text-ink">
                <span aria-hidden="true" className="text-accent">
                  &#8212;
                </span>
                <span>{step}</span>
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </div>
  );
}
