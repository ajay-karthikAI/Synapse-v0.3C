import type { AnswerEnvelope } from "@/lib/envelope";

import { ClaimList } from "@/components/answer/ClaimList";
import { ExcerptList } from "@/components/answer/ExcerptList";
import { InsufficientBody } from "@/components/answer/InsufficientCard";
import { SourceList } from "@/components/answer/SourceList";

/**
 * A validated answer, in all three of the states one can be in.
 *
 * `action` distinguishes them and they are genuinely different screens:
 *
 * - **`answer`** — claims survived verification. The ordinary result.
 * - **`abstain`** — every claim failed verification and was withheld, so the
 *   insufficient-evidence card is shown *inside* the answer. The questions and
 *   limitations survive: the system still knows what is worth asking even when
 *   it cannot tell you anything itself.
 * - **`medical_staff`** — the model routed the question to a person instead of
 *   answering it, and `staff_message` is the application's copy for that.
 *
 * Ordering is a safety decision, not a layout preference. The summary, then
 * what was found, then what to ask, then the caveats, then the evidence. The
 * disclaimer is last and always present — it arrives on the envelope from
 * `synapse.answer.render.PERMANENT_DISCLAIMER` and is never composed here.
 *
 * Every dynamic value below is JSX text. There is no `dangerouslySetInnerHTML`
 * in this file or anywhere under `src/`, and a unit test asserts that.
 */

interface AnswerCardProps {
  envelope: AnswerEnvelope;
  turnKey: string;
  headingId: string;
  headingRef: React.Ref<HTMLHeadingElement>;
}

export function AnswerCard({ envelope, turnKey, headingId, headingRef }: AnswerCardProps) {
  const abstained = envelope.action === "abstain";
  const staffReferral = envelope.action === "medical_staff";
  const partial = envelope.claims.some((claim) => claim.support === "partially_supported");

  return (
    <article aria-labelledby={headingId} className="mt-8">
      {/*
        On an abstention the insufficient card supplies the heading itself, at
        level 2, and becomes the focus target. Rendering an `h2` here as well
        would print the server's own "Not enough verified information" twice in
        a row — once as the section heading and once as the card's — which reads
        as a rendering bug and gives a screen-reader user two headings that say
        the same thing.
      */}
      {abstained ? null : (
        <h2
          id={headingId}
          ref={headingRef}
          // Focused when the turn completes, so the next Tab starts at the
          // result. -1 keeps it out of the sequential order otherwise.
          tabIndex={-1}
          className="font-display text-[1.6rem] leading-snug font-normal text-display focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-accent"
        >
          {staffReferral ? "This one is for a person, not this tool" : "What the research says"}
        </h2>
      )}

      {/* The follow-up disclosure. Model-generated, so it is rendered as text
          and framed as an interpretation the patient can correct. */}
      {envelope.resolved_query ? (
        <p className="mt-3 rounded-[12px] border border-rule bg-sunken/60 px-4 py-3 text-[13px] leading-relaxed text-ink-secondary">
          Read as a follow-up. Searched for:{" "}
          <span className="text-ink">{envelope.resolved_query}</span>. If that is
          not what you meant, ask again in full.
        </p>
      ) : null}

      {staffReferral && envelope.staff_message ? (
        <p className="mt-5 rounded-[14px] border border-accent-soft/30 bg-accent-wash px-4 py-3.5 text-[15px] leading-relaxed text-ink">
          {envelope.staff_message}
        </p>
      ) : null}

      {envelope.summary ? (
        <p className="mt-5 text-[17px] leading-relaxed text-ink">{envelope.summary}</p>
      ) : null}

      {partial ? (
        <p role="note" className="mt-4 text-[13px] leading-relaxed text-warning">
          Some statements below are only partly supported by the passage they
          cite. They are marked, and the passage is shown.
        </p>
      ) : null}

      <ClaimList claims={envelope.claims} turnKey={turnKey} />

      {/* The abstention IS the insufficient state, carried inside the answer,
          and on that path it owns the result heading — see above. */}
      {abstained && envelope.insufficient ? (
        <div className="mt-2">
          <InsufficientBody
            detail={envelope.insufficient}
            headingLevel={2}
            headingId={headingId}
            headingRef={headingRef}
          />
        </div>
      ) : null}

      {envelope.doctor_evaluation ? (
        <Section title="What your clinician will weigh up" turnKey={turnKey} slug="evaluation">
          <p className="text-[15px] leading-relaxed text-ink">{envelope.doctor_evaluation}</p>
        </Section>
      ) : null}

      {envelope.questions_for_doctor.length > 0 ? (
        <Section title="Worth asking at your appointment" turnKey={turnKey} slug="questions">
          <ul className="space-y-2.5">
            {envelope.questions_for_doctor.map((question) => (
              <li
                key={question}
                className="flex gap-2.5 text-[15px] leading-relaxed text-ink"
              >
                <span aria-hidden="true" className="text-accent">
                  &#8212;
                </span>
                <span>{question}</span>
              </li>
            ))}
          </ul>
        </Section>
      ) : null}

      {envelope.limitations.length > 0 ? (
        <Section title="What this does not cover" turnKey={turnKey} slug="limitations">
          <ul className="space-y-2 text-[14px] leading-relaxed text-ink-secondary">
            {envelope.limitations.map((limitation) => (
              <li key={limitation}>{limitation}</li>
            ))}
          </ul>
        </Section>
      ) : null}

      <SourceList sources={envelope.sources} turnKey={turnKey} />
      <ExcerptList excerpts={envelope.excerpts} turnKey={turnKey} />

      <p className="mt-8 border-t border-rule pt-5 text-[13px] leading-relaxed text-ink-secondary">
        {envelope.disclaimer}
      </p>
    </article>
  );
}

function Section({
  title,
  slug,
  turnKey,
  children,
}: {
  title: string;
  slug: string;
  turnKey: string;
  children: React.ReactNode;
}) {
  const id = `${turnKey}-${slug}`;
  return (
    <section aria-labelledby={id} className="mt-8">
      <h3
        id={id}
        className="text-[11px] font-semibold uppercase tracking-[0.18em] text-ink-secondary"
      >
        {title}
      </h3>
      <div className="mt-3.5">{children}</div>
    </section>
  );
}
