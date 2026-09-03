import Link from "next/link";

/**
 * The 404 state.
 *
 * Says what happened and offers one way back. It does not guess what the caller
 * meant, and it does not echo the requested path — reflecting a URL into the
 * page is how a 404 becomes a phishing surface.
 */
export default function NotFound() {
  return (
    <div className="mx-auto max-w-2xl px-4 py-16 text-center sm:px-6 sm:py-24">
      <p className="text-sm font-semibold uppercase tracking-[0.14em] text-ink-secondary">
        Page not found
      </p>
      <h1 className="font-display mt-3 text-[2.25rem] font-semibold leading-tight text-display">
        That page does not exist
      </h1>
      <p className="mt-4 leading-relaxed text-ink-secondary">
        The link may be out of date, or the address may have been mistyped.
      </p>
      <Link
        href="/"
        className="mt-8 inline-flex min-h-[44px] items-center rounded-[12px] bg-accent-royal px-5 py-3 font-medium text-white no-underline transition-colors hover:bg-[#8B5CF6]"
      >
        Go to the start
      </Link>
    </div>
  );
}
