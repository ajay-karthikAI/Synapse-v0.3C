import Link from "next/link";

/**
 * The permanent footer.
 *
 * Quiet: a rule, a paragraph, two links. The disclaimer here is the standing
 * statement about the product. It is NOT the same string as the one rendered
 * beneath an answer — that one comes from
 * `synapse.answer.render.PERMANENT_DISCLAIMER`, is emitted by the server with
 * every answer, and is never composed in the browser. Neither substitutes for
 * the other.
 *
 * The attribution sits on its own line, away from the disclaimer's claim, so a
 * second organisation's name cannot read as a co-signature on it.
 */
export function SiteFooter() {
  return (
    <footer className="mt-auto border-t border-rule">
      <div className="mx-auto max-w-3xl px-5 py-10">
        <p className="text-center text-[13px] leading-relaxed text-ink-secondary">
          Synapse is a pre-clinical research prototype. It is not a medical
          device, has not been clinically validated, and must not be used to
          make decisions about your care. If you think you may have a medical
          emergency, contact your local emergency number.
        </p>
        <div className="mt-5 flex flex-col items-center gap-3 text-center">
          <Link
            href="/transparency"
            className="text-[13px] text-accent underline-offset-4 hover:underline"
          >
            About Synapse
          </Link>
        </div>
      </div>
    </footer>
  );
}
