"use client";

import { useRef } from "react";

/**
 * The question field.
 *
 * **Free text is unrestricted.** There is no vocabulary filter, no forbidden
 * word list and no structured symptom picker, because a patient has to be able
 * to describe what is happening in their own words — a box that rejects the
 * phrasing someone reaches for teaches them to stop describing. The only bound
 * is length, which matches the server's `MAX_QUERY_CHARS`. The identifier
 * warning is the control, and it is permanent rather than dismissible.
 *
 * **The draft survives everything.** The value lives in the parent, not here,
 * so a failed turn, an expired session or a dropped connection leaves the text
 * exactly where the patient typed it. Clearing an input on error is how someone
 * loses a carefully worded description of a symptom and gives up.
 *
 * **Submission cannot be duplicated.** `busy` disables the control and the
 * submit handler returns early — belt and braces, because a disabled button
 * still fires on Enter in some browsers, and a second turn on one session is
 * refused by the server anyway (409) after having cost a slot against the
 * question ceiling.
 */

export const MAX_CHARS = 2000;

interface ComposerProps {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  busy: boolean;
  examples: readonly string[];
  /** Hides the chips once a conversation is under way. */
  showExamples: boolean;
  /**
   * Presents the field as a continuation rather than a beginning.
   *
   * Only the presentation changes. The accessible label, the control names and
   * the submit contract are identical, because this is the same field doing the
   * same thing — a second one would mean two elements sharing `id="question"`,
   * and a screen reader would find two "Your question or symptoms" boxes with
   * no way to tell which is live.
   */
  followUp?: boolean;
  /** Focus and label the field from outside, e.g. after a retry. */
  fieldRef?: React.RefObject<HTMLTextAreaElement | null>;
}

export function Composer({
  value,
  onChange,
  onSubmit,
  busy,
  examples,
  showExamples,
  followUp = false,
  fieldRef,
}: ComposerProps) {
  const fallback = useRef<HTMLTextAreaElement>(null);
  const field = fieldRef ?? fallback;
  const remaining = MAX_CHARS - value.length;
  const empty = value.trim().length === 0;

  function grow(element: HTMLTextAreaElement) {
    element.style.height = "auto";
    element.style.height = `${Math.min(element.scrollHeight, 260)}px`;
  }

  function fillWith(example: string) {
    onChange(example);
    const element = field.current;
    if (element) {
      element.focus();
      requestAnimationFrame(() => grow(element));
    }
  }

  return (
    <div className="mx-auto max-w-xl">
      {followUp ? (
        <div className="mb-4">
          <h2 className="text-[11px] font-semibold uppercase tracking-[0.18em] text-ink-secondary">
            Ask a follow-up
          </h2>
          <p className="mt-1.5 text-[13px] leading-relaxed text-ink-secondary">
            This session stays open, so a follow-up is read in context — you can
            ask &ldquo;what about the side effects?&rdquo; without repeating
            yourself. A new question is fine here too.
          </p>
        </div>
      ) : null}

      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (busy || empty) return;
          onSubmit();
        }}
      >
        <label htmlFor="question" className="visually-hidden">
          Your question or symptoms
        </label>
        <div className="rounded-[16px] border border-border bg-surface shadow-[0_1px_2px_rgb(0_0_0_/_0.2)] transition-shadow focus-within:border-accent">
          <textarea
            id="question"
            ref={field}
            rows={2}
            maxLength={MAX_CHARS}
            value={value}
            disabled={busy}
            onChange={(event) => {
              onChange(event.target.value);
              grow(event.target);
            }}
            onKeyDown={(event) => {
              // Enter sends; Shift+Enter makes a new line. The hint below says so.
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                if (!busy && !empty) onSubmit();
              }
            }}
            placeholder={
              followUp
                ? "Ask a follow-up, or something new"
                : "What would you like to understand?"
            }
            aria-describedby="question-help question-count"
            className="block w-full resize-none rounded-t-[16px] bg-transparent px-5 pt-4 pb-2 text-[17px] leading-relaxed text-ink outline-none placeholder:text-ink-secondary/70 disabled:opacity-60"
          />
          <div className="flex items-center justify-between gap-3 px-3 pb-3">
            <span
              id="question-count"
              // Polite and only near the limit: announcing a count on every
              // keystroke makes the field unusable with a screen reader.
              aria-live={remaining <= 100 ? "polite" : "off"}
              className={`pl-2 text-xs ${remaining <= 100 ? "text-warning" : "text-ink-secondary"}`}
            >
              {remaining <= 100
                ? `${remaining} characters left of ${MAX_CHARS}`
                : "Enter to send, Shift and Enter for a new line"}
            </span>
            <button
              type="submit"
              disabled={busy || empty}
              className="inline-flex min-h-[44px] items-center gap-2 rounded-[12px] bg-accent-royal px-5 text-[15px] font-medium text-white transition-colors hover:bg-[#8B5CF6] disabled:cursor-not-allowed disabled:opacity-40"
            >
              {busy ? "Working" : "Ask"}
              <span aria-hidden="true">{busy ? "" : "→"}</span>
            </button>
          </div>
        </div>
        <p id="question-help" className="visually-hidden">
          Describe what you want to understand before your appointment. Plain
          language is fine. Do not include anything that identifies you.
        </p>
      </form>

      {showExamples ? (
        <>
          <h2
            id="examples-heading"
            className="mt-6 text-center text-[11px] font-semibold uppercase tracking-[0.18em] text-ink-secondary"
          >
            Or start from one of these
          </h2>
          <ul
            aria-labelledby="examples-heading"
            className="mt-3 flex flex-wrap justify-center gap-2"
          >
            {examples.map((example) => (
              <li key={example}>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => fillWith(example)}
                  className="min-h-[44px] rounded-[12px] border border-rule bg-surface px-3.5 py-2 text-left text-[13px] leading-snug text-ink-secondary transition-colors hover:border-accent-soft hover:bg-accent-wash hover:text-accent disabled:opacity-40"
                >
                  {example}
                </button>
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </div>
  );
}
