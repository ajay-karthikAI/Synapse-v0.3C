"use client";

/**
 * The last resort: an error in the root layout itself.
 *
 * This replaces `<html>`, so it cannot use the site chrome, the fonts, or the
 * stylesheet — anything it depends on might be the thing that failed. The
 * styles are therefore inline and minimal, and the copy is the same fixed
 * message the route boundary uses.
 */
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <html lang="en">
      <body
        style={{
          margin: 0,
          minHeight: "100vh",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          backgroundColor: "#423E3E",
          color: "#FFFFFF",
          fontFamily: "ui-sans-serif, system-ui, -apple-system, sans-serif",
          padding: "2rem",
        }}
      >
        <div role="alert" style={{ maxWidth: "34rem" }}>
          <h1 style={{ fontSize: "1.25rem", fontWeight: 600, color: "#FCA5A5" }}>
            Something went wrong
          </h1>
          <p style={{ marginTop: "0.75rem", lineHeight: 1.6 }}>
            Synapse could not load. Nothing you entered has been saved. If you
            need help now, please speak to a member of staff.
          </p>
          {error.digest ? (
            <p style={{ marginTop: "1rem", fontSize: "0.875rem", color: "#D6D1DA" }}>
              Reference code: {error.digest}
            </p>
          ) : null}
          <button
            type="button"
            onClick={reset}
            style={{
              marginTop: "1.5rem",
              minHeight: 44,
              borderRadius: 12,
              border: "1px solid #718C99",
              background: "transparent",
              color: "#FFFFFF",
              padding: "0.75rem 1.25rem",
              fontWeight: 500,
              cursor: "pointer",
            }}
          >
            Try again
          </button>
        </div>
      </body>
    </html>
  );
}
