/**
 * The identifier warning.
 *
 * Permanent and not dismissible: free text is unrestricted by design, so the
 * warning stays for as long as the field it applies to.
 *
 * **Placed near the foot of the page, above "What it will not do."** It used to
 * sit directly above the composer, where it was the first thing a patient read
 * and the largest object on an otherwise calm screen — a caution given the
 * weight of an instruction. It reads better as a standing disclaimer than as a
 * gate in front of the question box.
 *
 * That move costs nothing for a screen reader, and the reason is worth stating
 * so it is not undone by accident: the composer's textarea carries its own
 * `aria-describedby` help text — "Do not include anything that identifies you"
 * — which is announced at the field itself. The visual notice and the announced
 * one are separate on purpose, so moving one does not silence the other.
 *
 * A heading level 2, matching the other section headings on the page, so the
 * document outline stays flat and nothing is skipped.
 */
export function IdentifierNotice() {
  return (
    <div className="mx-auto w-full max-w-3xl px-5">
      <div
        role="note"
        className="mx-auto max-w-xl rounded-[16px] border border-warning/25 bg-warning-surface px-5 py-4"
      >
        <h2 className="text-sm font-semibold text-warning">
          Do not enter anything that identifies you
        </h2>
        <p className="mt-1.5 text-sm leading-relaxed text-ink">
          No name, date of birth, contact details or record number. Your question
          is sent to a model provider to be answered, and this prototype has no
          approval to handle personal health information. It is research
          software, not a medical device, and it has not been clinically
          validated.
        </p>
      </div>
    </div>
  );
}
