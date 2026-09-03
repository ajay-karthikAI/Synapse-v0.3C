import type { EmergencyEnvelope } from "@/lib/envelope";

/**
 * A red flag. The one state that is allowed to interrupt.
 *
 * `role="alert"` appears exactly once in this application, here. That is not
 * stylistic restraint — it is what makes the role work. An alert interrupts
 * whatever a screen reader is currently saying, so a page where several things
 * claim to be alerts is a page where the one that matters is queued behind a
 * loading message. Failures, insufficient evidence and every other state use
 * ordinary structure and a polite live region; only this pre-empts.
 *
 * The envelope is produced *before* retrieval and generation. It carries no
 * claims, no sources and no brief, and the type reflects that: there is no
 * field here through which evidence could arrive. That ordering is the
 * invariant — the emergency check precedes retrieval — and the shape of this
 * component is downstream of it.
 *
 * `message` is application copy from `synapse.answer.render.EMERGENCY_MESSAGE`.
 * No model contributes a word to this screen.
 */

interface EmergencyCardProps {
  envelope: EmergencyEnvelope;
  headingId: string;
  headingRef: React.Ref<HTMLHeadingElement>;
}

export function EmergencyCard({ envelope, headingId, headingRef }: EmergencyCardProps) {
  return (
    <article
      // The alert. Present in the DOM from the moment the turn resolves, so the
      // role fires — a container that already existed and merely changed text
      // is announced far less reliably.
      role="alert"
      aria-labelledby={headingId}
      className="mt-8 rounded-[16px] border-2 border-emergency/60 bg-emergency-surface px-5 py-5"
    >
      <h2
        id={headingId}
        ref={headingRef}
        tabIndex={-1}
        className="font-display text-[1.6rem] leading-snug font-normal text-emergency focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-accent"
      >
        Stop and get medical help now
      </h2>

      <p className="mt-4 text-[16px] leading-relaxed text-ink">{envelope.message}</p>

      <p className="mt-5 border-t border-emergency/30 pt-4 text-[13px] leading-relaxed text-ink-secondary">
        Synapse has not searched for research on this and has nothing to show
        you. It is not able to judge how urgent a symptom is — this is a fixed
        response to the words you used, not an assessment of your condition.
      </p>

      <p className="mt-4 text-[13px] leading-relaxed text-ink-secondary">
        {envelope.disclaimer}
      </p>
    </article>
  );
}
