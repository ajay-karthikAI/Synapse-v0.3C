/**
 * The first focusable element on the page.
 *
 * The Streamlit interface could not make this the first tab stop — the
 * framework rendered its own chrome ahead of any application markup, and that
 * was recorded as an unresolvable limitation in docs/accessibility.md §6. Here
 * it is simply the first element in the body, which is the whole point of
 * owning the document.
 *
 * Visually hidden until focused, then a real, visible control.
 */
export function SkipLink() {
  return (
    <a
      href="#main"
      className="visually-hidden focus-visible:absolute focus-visible:left-4 focus-visible:top-4 focus-visible:z-50 focus-visible:rounded-[12px] focus-visible:bg-surface focus-visible:border focus-visible:border-accent focus-visible:px-4 focus-visible:py-3 focus-visible:text-sm focus-visible:font-medium focus-visible:text-display focus-visible:shadow-[0_2px_4px_rgb(16_42_58_/_0.05),0_4px_12px_rgb(16_42_58_/_0.07)]"
    >
      Skip to main content
    </a>
  );
}
