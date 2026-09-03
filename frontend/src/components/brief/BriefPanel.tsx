"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";

import type { Brief } from "@/lib/envelope";
import {
  EXPORT_FORMATS,
  addBriefQuestion,
  exportUrl,
  readBrief,
  removeBriefQuestion,
  reorderBriefQuestions,
  setBriefNotes,
  setBriefSections,
  setBriefTopic,
} from "@/lib/session-client";
import { DESKTOP_QUERY, useMediaQuery } from "@/lib/useMediaQuery";

/**
 * The appointment brief: one page to take to a clinician.
 *
 * **The server owns it.** Every edit is one named operation on one field, and
 * the response replaces local state wholesale. There is no code path here that
 * sends a brief — see `synapse/api/routes/brief.py` for why: a brief carries
 * verified claims with their support levels, and an endpoint that accepted a
 * whole one would let a caller print unverified claims on a document a patient
 * hands to a doctor. So the client can change the topic, the notes, its own
 * questions and which sections to include, and nothing else.
 *
 * **Two different components, one file.** On a wide screen this is a side panel
 * next to the answer: it does not cover the page, so it is a `complementary`
 * region, does not trap focus, and leaves the rest of the document operable. On
 * a narrow screen the identical content fills the viewport, which makes it a
 * modal `dialog`: focus is trapped, Escape dismisses it, and the page behind is
 * hidden from assistive technology. Presenting a full-screen overlay as a
 * non-modal region strands a screen-reader user underneath it.
 *
 * **Reordering is buttons, not dragging.** Move up and move down work from the
 * keyboard, on a touch screen, and with a screen reader. A drag handle works
 * with a mouse. The list announces its new position after each move.
 */

interface BriefPanelProps {
  turnIndex: number;
  open: boolean;
  onClose: () => void;
}

export function BriefPanel({ turnIndex, open, onClose }: BriefPanelProps) {
  const desktop = useMediaQuery(DESKTOP_QUERY);
  const [brief, setBrief] = useState<Brief | null>(null);
  const [error, setError] = useState(false);
  const [announcement, setAnnouncement] = useState("");
  const panel = useRef<HTMLDivElement>(null);
  const titleId = useId();

  // Loaded on open. The server builds it once per turn and caches it, so
  // reopening does not issue a new document identifier.
  useEffect(() => {
    if (!open) return;
    let live = true;
    void readBrief(turnIndex).then((loaded) => {
      if (!live) return;
      if (loaded) setBrief(loaded);
      else setError(true);
    });
    return () => {
      live = false;
    };
  }, [open, turnIndex]);

  // Focus moves into the panel when it opens, and back to whatever opened it
  // when it closes — otherwise focus is left on a button that is now gone and
  // the next Tab starts from the top of the document.
  useEffect(() => {
    if (!open) return;
    const restoreTo = document.activeElement as HTMLElement | null;
    panel.current?.focus();
    return () => restoreTo?.focus?.();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
        return;
      }
      // The focus trap, and only on mobile: a non-modal side panel that stole
      // Tab would make the rest of the page unreachable by keyboard.
      if (event.key !== "Tab" || desktop || !panel.current) return;
      const focusable = panel.current.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
      );
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open, desktop, onClose]);

  /** Apply one server edit, replacing local state with whatever comes back. */
  const apply = useCallback(async (operation: Promise<Brief | null>, message: string) => {
    const updated = await operation;
    if (updated) {
      setBrief(updated);
      setAnnouncement(message);
    } else {
      setError(true);
    }
  }, []);

  if (!open) return null;

  const modal = !desktop;

  return (
    <>
      {/* The scrim exists only in the modal case. Clicking it dismisses, and it
          is aria-hidden because it is not a control a screen reader can use —
          Escape is the equivalent, and it works. */}
      {modal ? (
        <div
          aria-hidden="true"
          onClick={onClose}
          className="fixed inset-0 z-40 bg-black/60 backdrop-blur-sm"
        />
      ) : null}

      <div
        ref={panel}
        tabIndex={-1}
        {...(modal
          ? { role: "dialog" as const, "aria-modal": true }
          : { role: "complementary" as const })}
        aria-labelledby={titleId}
        data-testid="brief-panel"
        className={
          modal
            ? "fixed inset-0 z-50 flex flex-col overflow-y-auto bg-canvas px-5 pb-8 pt-5 focus-visible:outline-none"
            : "mt-6 rounded-[16px] border border-rule bg-surface p-6 focus-visible:outline-none"
        }
      >
        <div className="flex items-start justify-between gap-4">
          <div>
            <h3
              id={titleId}
              className="font-display text-[1.4rem] leading-snug font-normal text-display"
            >
              Your appointment brief
            </h3>
            <p className="mt-1.5 text-[13px] leading-relaxed text-ink-secondary">
              One page to take with you. Nothing here is stored — it exists until
              you close this session.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="min-h-[44px] shrink-0 rounded-[12px] border border-rule px-3.5 text-[14px] text-ink-secondary transition-colors hover:text-display"
          >
            {modal ? "Close" : "Hide"}
          </button>
        </div>

        {/* Every edit result is announced here rather than by moving focus, so
            a change does not interrupt what someone is typing. */}
        <p role="status" aria-live="polite" className="visually-hidden">
          {announcement}
        </p>

        {error && !brief ? (
          <p className="mt-6 text-[14px] leading-relaxed text-ink">
            The brief could not be loaded. Your answer above is unaffected.
          </p>
        ) : null}

        {brief ? (
          <BriefBody brief={brief} turnIndex={turnIndex} apply={apply} />
        ) : (
          !error && (
            <p role="status" className="mt-6 text-[14px] text-ink-secondary">
              Building your brief...
            </p>
          )
        )}
      </div>
    </>
  );
}

interface BriefBodyProps {
  brief: Brief;
  turnIndex: number;
  apply: (operation: Promise<Brief | null>, message: string) => Promise<void>;
}

function BriefBody({ brief, turnIndex, apply }: BriefBodyProps) {
  const [topic, setTopic] = useState(brief.topic);
  const [notes, setNotes] = useState(brief.notes);
  const [question, setQuestion] = useState("");

  return (
    <div className="mt-6 space-y-8">
      {/* --- What the patient wrote themselves --- */}
      <div>
        <label
          htmlFor="brief-topic"
          className="block text-[11px] font-semibold uppercase tracking-[0.18em] text-ink-secondary"
        >
          What you want to talk about
        </label>
        <input
          id="brief-topic"
          type="text"
          value={topic}
          maxLength={2000}
          onChange={(event) => setTopic(event.target.value)}
          // Committed on blur, not per keystroke: one request per character
          // would be a request per character containing the patient's own words.
          onBlur={() => {
            if (topic !== brief.topic) void apply(setBriefTopic(turnIndex, topic), "Topic saved");
          }}
          placeholder="In one line"
          className="mt-2.5 block min-h-[44px] w-full rounded-[12px] border border-border bg-surface px-3.5 text-[15px] text-ink outline-none focus:border-accent"
        />
      </div>

      <div>
        <label
          htmlFor="brief-notes"
          className="block text-[11px] font-semibold uppercase tracking-[0.18em] text-ink-secondary"
        >
          Your notes
        </label>
        <textarea
          id="brief-notes"
          rows={3}
          value={notes}
          maxLength={4000}
          onChange={(event) => setNotes(event.target.value)}
          onBlur={() => {
            if (notes !== brief.notes) void apply(setBriefNotes(turnIndex, notes), "Notes saved");
          }}
          placeholder="What you have noticed, and when it started"
          className="mt-2.5 block w-full resize-y rounded-[12px] border border-border bg-surface px-3.5 py-2.5 text-[15px] leading-relaxed text-ink outline-none focus:border-accent"
        />
      </div>

      {/* --- Questions: add, remove, reorder --- */}
      <div>
        <h4 className="text-[11px] font-semibold uppercase tracking-[0.18em] text-ink-secondary">
          Questions to ask
        </h4>
        <ol className="mt-3 space-y-2">
          {brief.questions.map((item, index) => (
            <li
              key={item.question_id}
              className="flex items-start gap-2 rounded-[12px] border border-rule bg-sunken/50 px-3 py-2.5"
            >
              <span className="min-w-0 flex-1 text-[14px] leading-relaxed text-ink">
                {item.text}
                {item.origin === "user" ? (
                  <span className="ml-2 text-[11px] text-ink-secondary">(yours)</span>
                ) : null}
              </span>
              <span className="flex shrink-0 gap-1">
                <MoveButton
                  label={`Move "${item.text}" up`}
                  disabled={index === 0}
                  glyph="↑"
                  onClick={() =>
                    apply(
                      reorderBriefQuestions(turnIndex, swap(idsOf(brief), index, index - 1)),
                      `Moved to position ${index}`,
                    )
                  }
                />
                <MoveButton
                  label={`Move "${item.text}" down`}
                  disabled={index === brief.questions.length - 1}
                  glyph="↓"
                  onClick={() =>
                    apply(
                      reorderBriefQuestions(turnIndex, swap(idsOf(brief), index, index + 1)),
                      `Moved to position ${index + 2}`,
                    )
                  }
                />
                <MoveButton
                  label={`Remove "${item.text}"`}
                  glyph="×"
                  onClick={() =>
                    apply(removeBriefQuestion(turnIndex, item.question_id), "Question removed")
                  }
                />
              </span>
            </li>
          ))}
        </ol>

        <form
          className="mt-3 flex gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            const text = question.trim();
            if (!text) return;
            setQuestion("");
            void apply(addBriefQuestion(turnIndex, text), "Question added");
          }}
        >
          <label htmlFor="brief-new-question" className="visually-hidden">
            Add a question of your own
          </label>
          <input
            id="brief-new-question"
            type="text"
            value={question}
            maxLength={500}
            onChange={(event) => setQuestion(event.target.value)}
            placeholder="Add your own question"
            className="min-h-[44px] flex-1 rounded-[12px] border border-border bg-surface px-3.5 text-[14px] text-ink outline-none focus:border-accent"
          />
          <button
            type="submit"
            disabled={question.trim().length === 0}
            className="min-h-[44px] rounded-[12px] bg-accent-royal px-4 text-[14px] font-medium text-white disabled:opacity-40"
          >
            Add
          </button>
        </form>
      </div>

      {/* --- Which sections the export carries --- */}
      <fieldset>
        <legend className="text-[11px] font-semibold uppercase tracking-[0.18em] text-ink-secondary">
          Include on the page
        </legend>
        <div className="mt-3 space-y-2">
          {brief.available_sections.map((section) => {
            const checked = brief.included_sections.includes(section);
            return (
              <label
                key={section}
                className="flex min-h-[44px] items-center gap-2.5 text-[14px] text-ink"
              >
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() =>
                    apply(
                      setBriefSections(
                        turnIndex,
                        checked
                          ? brief.included_sections.filter((value) => value !== section)
                          : [...brief.included_sections, section],
                      ),
                      `${sectionLabel(section)} ${checked ? "removed" : "added"}`,
                    )
                  }
                  className="h-4 w-4 accent-[#7E3FF2]"
                />
                {sectionLabel(section)}
              </label>
            );
          })}
        </div>
        <p className="mt-2 text-[12px] leading-relaxed text-ink-secondary">
          The disclaimer is always printed and cannot be removed.
        </p>
      </fieldset>

      {/* --- Fit --- */}
      {!brief.fits_one_page && brief.overflow_advice.length > 0 ? (
        <div
          role="note"
          className="rounded-[12px] border border-warning/25 bg-warning-surface px-4 py-3"
        >
          <h4 className="text-[13px] font-semibold text-warning">
            This is longer than one page
          </h4>
          <ul className="mt-2 space-y-1.5 text-[13px] leading-relaxed text-ink">
            {brief.overflow_advice.map((advice) => (
              <li key={advice}>{advice}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {/* --- Export --- */}
      <div className="border-t border-rule pt-5">
        <h4 className="text-[11px] font-semibold uppercase tracking-[0.18em] text-ink-secondary">
          Take it with you
        </h4>
        <ul className="mt-3 flex flex-wrap gap-2">
          {EXPORT_FORMATS.map((format) => (
            <li key={format}>
              <a
                href={exportUrl(turnIndex, format)}
                // A real link, so the browser saves it using the server's
                // Content-Disposition. `download` is not set: the filename is
                // the server's to choose, and it builds one from the brief's
                // own document id.
                className="inline-flex min-h-[44px] items-center rounded-[12px] border border-rule bg-surface px-4 text-[14px] text-accent no-underline transition-colors hover:border-accent-soft hover:bg-accent-wash"
              >
                Download {format === "text" ? "plain text" : format.toUpperCase()}
              </a>
            </li>
          ))}
        </ul>
        <p className="mt-3 text-[12px] leading-relaxed text-ink-secondary">
          {brief.export_warning}
        </p>
        <p className="mt-3 text-[12px] leading-relaxed text-ink-secondary">
          {brief.disclaimer}
        </p>
      </div>
    </div>
  );
}

function MoveButton({
  label,
  glyph,
  onClick,
  disabled,
}: {
  label: string;
  glyph: string;
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled ?? false}
      // The glyph is decorative; the accessible name is the sentence.
      aria-label={label}
      className="inline-flex h-9 w-9 items-center justify-center rounded-[10px] border border-rule text-[14px] text-ink-secondary transition-colors hover:text-display disabled:opacity-30"
    >
      <span aria-hidden="true">{glyph}</span>
    </button>
  );
}

const idsOf = (brief: Brief) => brief.questions.map((question) => question.question_id);

/** Swap two positions, returning a new array. Out-of-range is a no-op. */
function swap(ids: readonly string[], from: number, to: number): string[] {
  const next = [...ids];
  const a = next[from];
  const b = next[to];
  if (a === undefined || b === undefined) return next;
  next[from] = b;
  next[to] = a;
  return next;
}

/**
 * Section identifiers are server enum values; these are the patient's words.
 *
 * Deliberately NOT the same wording as the editing fields above. "What you want
 * to talk about" names both a textbox and its include-checkbox otherwise, and
 * two controls with one accessible name inside a single panel is ambiguous to
 * anyone navigating by name — "topic, checkbox" and "topic, edit" is the
 * distinction that has to survive. The fieldset's legend supplies the "include
 * on the page" half of the meaning.
 */
function sectionLabel(section: string): string {
  const labels: Record<string, string> = {
    topic: "Topic",
    notes: "Notes",
    questions: "Questions",
    claims: "Research findings",
    sources: "Sources",
  };
  return labels[section] ?? section;
}
