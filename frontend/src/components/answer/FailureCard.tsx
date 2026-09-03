import type { FailureEnvelope } from "@/lib/envelope";

/**
 * Something broke. Fixed copy, a typed code, and nothing else.
 *
 * The envelope carries exactly two strings: `message`, which is
 * `synapse.ui.errors.PATIENT_ERROR_MESSAGE` and is byte-identical for every one
 * of the eight failure codes, and `code` itself. There is no field for an
 * exception, a provider message, or a partial answer, so there is nothing here
 * to accidentally render — the safety property is enforced by the type, and
 * this component simply has nothing else available to it.
 *
 * The code is shown, small, because a patient reporting a problem to whoever
 * runs the deployment can quote it, and because hiding it would not make the
 * failure less real. It is a closed enum value like `generation_unavailable` —
 * not a message, and not derived from one.
 *
 * No `role="alert"`. A fault is not an emergency, and the two must not compete
 * for the same interruption. The turn list is a polite live region, which
 * announces this without cutting anything off.
 */

interface FailureCardProps {
  envelope: FailureEnvelope;
  headingId: string;
  headingRef: React.Ref<HTMLHeadingElement>;
  /** Re-runs the same question. The draft was preserved for exactly this. */
  onRetry?: (() => void) | undefined;
}

export function FailureCard({ envelope, headingId, headingRef, onRetry }: FailureCardProps) {
  return (
    <article
      aria-labelledby={headingId}
      className="mt-8 rounded-[16px] border border-rule bg-surface px-5 py-5"
    >
      <h2
        id={headingId}
        ref={headingRef}
        tabIndex={-1}
        className="font-display text-[1.6rem] leading-snug font-normal text-display focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-accent"
      >
        Something went wrong
      </h2>

      <p className="mt-3 text-[15px] leading-relaxed text-ink">{envelope.message}</p>

      <p className="mt-4 text-[13px] leading-relaxed text-ink-secondary">
        Nothing was saved and nothing was sent to your clinician. You can ask the
        same question again.
      </p>

      <div className="mt-5 flex flex-wrap items-center gap-4">
        {onRetry ? (
          <button
            type="button"
            onClick={onRetry}
            className="inline-flex min-h-[44px] items-center rounded-[12px] bg-accent-royal px-5 text-[15px] font-medium text-white transition-colors hover:bg-[#8B5CF6]"
          >
            Try again
          </button>
        ) : null}
        <p className="text-[12px] text-ink-secondary">
          Reference code: <span className="font-mono">{envelope.code}</span>
        </p>
      </div>
    </article>
  );
}
