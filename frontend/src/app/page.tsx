import type { Metadata } from "next";

import { Wordmark } from "@/components/brand/Wordmark";
import { IdentifierNotice } from "@/components/chat/IdentifierNotice";
import { Conversation } from "@/components/chat/Conversation";

export const metadata: Metadata = { title: "Ask" };

/**
 * The question surface.
 *
 * The copy is about *preparing for an appointment*, and it is careful never to
 * be about finding out what you have. That distinction is the product: a
 * symptom checker answers "what is wrong with me", which this system is not
 * permitted to do and has no evidence it could do safely. What it does is read
 * published research and turn it into things worth raising with a clinician —
 * so the promise on the page is "arrive with better questions", never "find
 * out what is wrong".
 *
 * The examples are the strongest signal of that, which is why they are here
 * rather than in the component. Each is a question about understanding
 * something before a visit; none describes a symptom and asks what it means.
 * They are synthetic, written for this prototype, and reviewed by nobody
 * clinical.
 */

/**
 * Three openers that model the kind of question this is for.
 *
 * Curated and synthetic. Deliberately not drawn from any real interaction, and
 * deliberately not symptom descriptions — an example is an instruction, and
 * "why does my chest hurt" as a suggested prompt would teach exactly the use
 * this system refuses.
 */
const EXAMPLES = [
  "What does my HbA1c number actually mean?",
  "What should I ask about starting metformin?",
  "Why might my blood pressure be higher at home?",
] as const;

export default function HomePage() {
  return (
    <div className="w-full pt-16 sm:pt-20">
      <div className="mx-auto w-full max-w-3xl px-5">
        <Wordmark size="hero" />

        <p className="mx-auto mt-8 mb-12 max-w-xl text-center text-lg leading-relaxed text-ink-secondary sm:text-xl">
          Ask about something before your appointment. Synapse searches published
          research and turns what it finds into questions worth raising with a
          clinician. It does not diagnose, and it will not tell you what you
          have.
        </p>
      </div>

      <Conversation examples={EXAMPLES} />

      {/* A standing disclaimer, not a gate in front of the question box.
          The composer announces its own identifier warning at the field via
          aria-describedby, so this placement changes what the page LOOKS
          like without changing what a screen reader hears while typing. */}
      <IdentifierNotice />

      <div className="mx-auto mt-12 max-w-xl px-5 pb-20 text-center">
        <h2 className="font-display text-lg font-semibold text-display">
          What it will not do
        </h2>
        <ul className="mt-4 space-y-2.5 text-[15px] leading-relaxed text-ink-secondary">
          <li>Diagnose, or tell you what you have.</li>
          <li>Tell you how urgent something is.</li>
          <li>Advise starting, stopping or changing a medicine.</li>
          <li>
            Answer when the evidence is thin — it says so instead, which is a
            result, not a failure.
          </li>
        </ul>
      </div>
    </div>
  );
}
