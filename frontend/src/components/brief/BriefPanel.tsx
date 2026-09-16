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
  setBriefSections,
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
 * hands to a doctor. So the client can change its own questions and which
 * sections to include, and nothing else. (The contract still accepts a topic
 * and notes; this panel no longer offers either -- see `BriefBody`.)
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
  open: boolean;
  onClose: () => void;
}

export function BriefPanel({ open, onClose }: BriefPanelProps) {
  const desktop = useMediaQuery(DESKTOP_QUERY);
  const [brief, setBrief] = useState<Brief | null>(null);
  const [error, setError] = useState(false);
  const [announcement, setAnnouncement] = useState("");
  const panel = useRef<HTMLDivElement>(null);
  const titleId = useId();

  // Loaded on open. The server holds one brief for the conversation and widens
  // it as turns are added, so reopening after another question shows the fuller
  // document under the same document identifier rather than issuing a new one.
  useEffect(() => {
    if (!open) return;
    let live = true;
    void readBrief().then((loaded) => {
      if (!live) return;
      if (loaded) setBrief(loaded);
      else setError(true);
    });
    return () => {
      live = false;
    };
  }, [open]);

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
          <BriefBody brief={brief} apply={apply} />
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
  apply: (operation: Promise<Brief | null>, message: string) => Promise<void>;
}

function BriefBody({ brief, apply }: BriefBodyProps) {
  const [question, setQuestion] = useState("");

  // The "what you want to talk about" line and the free-text notes box used to
  // open this panel. Both are gone: a patient who has just had their question
  // answered is being asked to write the question down again, and the notes box
  // was a blank page with no prompt. The panel now does one thing -- the
  // questions to take in -- and the summary comes from the conversation.
  //
  // The server still holds `topic` and `notes`, and `setBriefTopic`/
  // `setBriefNotes` still exist: the PDF renders each as nothing when empty, so
  // there is no dead section, and nothing had to change behind the contract to
  // take two inputs off the screen.
  return (
    <div className="mt-6 space-y-8">
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
                {/* The server sends the ContentOrigin value verbatim, and it
                    is "user_authored" -- this compared against "user" and so
                    never marked a single question as the patient's own. */}
                {item.origin === "user_authored" ? (
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
                      reorderBriefQuestions(swap(idsOf(brief), index, index - 1)),
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
                      reorderBriefQuestions(swap(idsOf(brief), index, index + 1)),
                      `Moved to position ${index + 2}`,
                    )
                  }
                />
                <MoveButton
                  label={`Remove "${item.text}"`}
                  glyph="×"
                  onClick={() =>
                    apply(removeBriefQuestion(item.question_id), "Question removed")
                  }
                />
              </span>
            </li>
          ))}
        </ol>

        <h5 className="mt-6 text-[14px] leading-relaxed text-ink">
          Would you like to add more questions for your physician?
        </h5>

        <form
          className="mt-2.5 flex gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            const text = question.trim();
            if (!text) return;
            setQuestion("");
            void apply(addBriefQuestion(text), "Question added");
          }}
        >
          {/* The visible heading above is not the input's label: a heading is
              not associated with a field, so a screen reader reaching the box
              by Tab would hear nothing. The accessible name stays here. */}
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

      {/* --- The add-ons ---
          Two toggles, not one checkbox per section. The panel used to list all
          eight and tick every one of them, which is how the brief became a
          nine-section engineering document: a patient was offered "claims",
          "sources" and "limitations" as separate decisions and had no reason to
          make any of them. The default is the recap; these two are what a
          patient might genuinely want added. */}
      <fieldset>
        <legend className="text-[11px] font-semibold uppercase tracking-[0.18em] text-ink-secondary">
          Add to the PDF
        </legend>
        <div className="mt-3 space-y-1">
          <AddOn
            label="Your conversation"
            description={
              brief.transcript_turn_count === 1
                ? "The question you asked and the answer you were given."
                : `All ${brief.transcript_turn_count} questions you asked, and the answers.`
            }
            sections={transcriptSections(brief)}
            brief={brief}
            apply={apply}
          />
          <AddOn
            label="The research behind it"
            description="The findings, their citation numbers, and the studies they came from."
            sections={researchSections(brief)}
            brief={brief}
            apply={apply}
          />
        </div>
        <p className="mt-2 text-[12px] leading-relaxed text-ink-secondary">
          The summary, your questions and the disclaimer are always included.
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

      {/* --- Export ---
          The PDF is the button; the other three are a footnote. All four are
          real links rather than scripted fetch-and-blob, so the browser saves
          each using the server's Content-Disposition. `download` is not set:
          the filename is the server's to choose, and it builds one from the
          brief's own document id. */}
      <div className="border-t border-rule pt-5">
        <h4 className="text-[11px] font-semibold uppercase tracking-[0.18em] text-ink-secondary">
          Take it with you
        </h4>
        <a
          href={exportUrl("pdf")}
          className="mt-3 inline-flex min-h-[48px] items-center rounded-[12px] bg-accent-royal px-5 text-[15px] font-medium text-white no-underline transition-opacity hover:opacity-90"
        >
          Download the PDF
        </a>
        <p className="mt-3 text-[12px] leading-relaxed text-ink-secondary">
          Also available as{" "}
          {OTHER_FORMATS.map((format, index) => (
            <span key={format}>
              {index > 0 ? ", " : ""}
              <a href={exportUrl(format)} className="text-accent underline-offset-4 hover:underline">
                {formatLabel(format)}
              </a>
            </span>
          ))}
          .
        </p>
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

/** The section the transcript add-on switches on. */
const TRANSCRIPT = "transcript";

/**
 * Which sections each add-on covers, DERIVED from the contract rather than
 * restated here.
 *
 * `available_sections` minus `default_sections` is exactly the set that is off
 * by default, so the two groups are a partition of it. Hard-coding the research
 * sections would mean this file and `synapse/brief/schema.py` could disagree
 * about what "the research" is, and the failure would be silent: a section the
 * server added would simply never be offered.
 */
function addOnSections(brief: Brief): string[] {
  return brief.available_sections.filter(
    (section) => !brief.default_sections.includes(section),
  );
}

function transcriptSections(brief: Brief): string[] {
  return addOnSections(brief).filter((section) => section === TRANSCRIPT);
}

function researchSections(brief: Brief): string[] {
  return addOnSections(brief).filter((section) => section !== TRANSCRIPT);
}

/** The formats offered below the PDF button. */
const OTHER_FORMATS = EXPORT_FORMATS.filter((format) => format !== "pdf");

function formatLabel(format: string): string {
  const labels: Record<string, string> = {
    html: "a web page",
    text: "plain text",
    json: "structured data",
  };
  return labels[format] ?? format;
}

/**
 * One add-on: a group of sections the patient turns on together.
 *
 * Checked only when every section in the group is included, so a partial state
 * arriving from the server reads as off rather than as on-and-broken.
 */
function AddOn({
  label,
  description,
  sections,
  brief,
  apply,
}: {
  label: string;
  description: string;
  sections: readonly string[];
  brief: Brief;
  apply: (operation: Promise<Brief | null>, message: string) => Promise<void>;
}) {
  if (sections.length === 0) return null;
  const checked = sections.every((section) => brief.included_sections.includes(section));
  const next = checked
    ? brief.included_sections.filter((section) => !sections.includes(section))
    : [...brief.included_sections, ...sections];

  return (
    <label className="flex min-h-[44px] cursor-pointer items-start gap-2.5 py-1.5 text-[14px] text-ink">
      <input
        type="checkbox"
        checked={checked}
        onChange={() =>
          apply(setBriefSections(next), `${label} ${checked ? "removed" : "added"}`)
        }
        className="mt-1 h-4 w-4 shrink-0 accent-[#7E3FF2]"
      />
      <span>
        {label}
        <span className="mt-0.5 block text-[12px] leading-relaxed text-ink-secondary">
          {description}
        </span>
      </span>
    </label>
  );
}
