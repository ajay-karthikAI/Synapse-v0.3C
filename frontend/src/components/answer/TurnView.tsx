"use client";

import { useEffect, useRef } from "react";

import { AnswerCard } from "@/components/answer/AnswerCard";
import { EmergencyCard } from "@/components/answer/EmergencyCard";
import { FailureCard } from "@/components/answer/FailureCard";
import { InsufficientCard } from "@/components/answer/InsufficientCard";
import { assertNever, type TurnEnvelope } from "@/lib/envelope";

/**
 * One completed turn: the question, then exactly one of four screens.
 *
 * The switch below is the point of the whole discriminated union. Every branch
 * narrows `envelope` to a single concrete type, and the `default` hands the
 * remaining value to `assertNever`, whose parameter is `never`. Add a fifth
 * envelope to the API and this file stops compiling — which is the only
 * mechanism that reliably prevents a new server state from rendering as a blank
 * region in a browser nobody was watching.
 *
 * **Focus.** When a turn completes, focus moves to its result heading. That is
 * what makes the answer reachable without hunting: the next Tab lands inside
 * the result rather than back at the top of the document. Because the heading
 * is what receives focus, a screen reader announces the heading and its level —
 * "What the research says, heading level two" — and not the page over again,
 * which is what moving focus to a container or re-rendering a live region would
 * produce.
 */

interface TurnViewProps {
  envelope: TurnEnvelope;
  /** The patient's own words, echoed above the result. */
  question: string;
  /** Distinct per turn: scopes every generated id so several turns can coexist. */
  turnKey: string;
  /** True only for the turn that just finished, so older ones do not steal focus. */
  autoFocus: boolean;
  onRetry?: (() => void) | undefined;
  /** Rendered under the result when this turn offers an appointment brief. */
  briefSlot?: React.ReactNode;
}

export function TurnView({
  envelope,
  question,
  turnKey,
  autoFocus,
  onRetry,
  briefSlot,
}: TurnViewProps) {
  const heading = useRef<HTMLHeadingElement>(null);
  const headingId = `${turnKey}-result`;

  useEffect(() => {
    if (!autoFocus) return;
    const target = heading.current;
    if (!target) return;

    // `preventScroll` because the browser's own scroll-into-view jumps the
    // heading to the very top edge; the scroll below places it with room above.
    target.focus({ preventScroll: true });

    // Guarded because focus is the part that matters and scrolling is the
    // nicety. `scrollIntoView` is absent in jsdom and in some embedded
    // browsers, and letting it throw here would take the whole result down
    // with it — an answer nobody can see, because it could not be centred.
    target.scrollIntoView?.({ block: "center", behavior: "smooth" });
  }, [autoFocus, turnKey]);

  return (
    <section
      aria-labelledby={`${turnKey}-question`}
      className="border-t border-rule pt-10 first:border-t-0 first:pt-0"
    >
      <h2
        id={`${turnKey}-question`}
        className="text-[11px] font-semibold uppercase tracking-[0.18em] text-ink-secondary"
      >
        Your question
      </h2>
      <p className="mt-2.5 text-[17px] leading-relaxed text-ink">{question}</p>

      {renderEnvelope()}
      {briefSlot}
    </section>
  );

  function renderEnvelope() {
    switch (envelope.kind) {
      case "answer":
        return (
          <AnswerCard
            envelope={envelope}
            turnKey={turnKey}
            headingId={headingId}
            headingRef={heading}
          />
        );
      case "emergency":
        return (
          <EmergencyCard envelope={envelope} headingId={headingId} headingRef={heading} />
        );
      case "insufficient":
        return (
          <InsufficientCard envelope={envelope} headingId={headingId} headingRef={heading} />
        );
      case "failure":
        return (
          <FailureCard
            envelope={envelope}
            headingId={headingId}
            headingRef={heading}
            onRetry={onRetry}
          />
        );
      default:
        // Unreachable while the union has four members. If it ever compiles
        // with a fifth, this line is the error that says so.
        return assertNever(envelope);
    }
  }
}
