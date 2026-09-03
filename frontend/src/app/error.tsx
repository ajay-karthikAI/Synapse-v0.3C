"use client";

import { useEffect } from "react";

/**
 * The route-level error boundary.
 *
 * It renders fixed copy and a digest, and nothing else. `error.message` is
 * never shown: on the server Next replaces it with a generic string in
 * production, but in development it is the real message — and a real message
 * from this application can quote a prompt, which contains a patient's
 * question. Rendering it would make the development build leak what the
 * production build carefully does not.
 *
 * `error.digest` is a server-generated identifier with no content in it, which
 * is exactly what a support conversation needs.
 */
export default function RouteError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // Type and digest only. Never the message, never the stack.
    console.error("route error", { digest: error.digest, name: error.name });
  }, [error]);

  return (
    <div className="mx-auto max-w-2xl px-4 py-16 sm:px-6 sm:py-24">
      <div
        role="alert"
        className="rounded-[16px] border border-emergency/25 bg-emergency-surface p-6 sm:p-8"
      >
        <h1 className="font-display text-2xl font-semibold text-emergency">Something went wrong</h1>
        <p className="mt-3 leading-relaxed text-ink">
          This page could not be displayed. Nothing you entered has been saved.
          If you need help now, please speak to a member of staff rather than
          waiting for this to work.
        </p>
        {error.digest ? (
          <p className="mt-4 text-sm text-ink-secondary">
            Reference code: <span className="font-mono">{error.digest}</span>
          </p>
        ) : null}
        <button
          type="button"
          onClick={reset}
          className="mt-6 min-h-[44px] rounded-[12px] border border-border bg-surface px-5 py-3 font-medium text-display transition-colors hover:bg-sunken"
        >
          Try again
        </button>
      </div>
    </div>
  );
}
