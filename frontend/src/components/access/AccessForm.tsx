"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

/**
 * The passcode form.
 *
 * The passcode goes to `/api/access/login` on this server, which forwards it to
 * FastAPI with the service token. It is never held in state longer than the
 * request, never placed in a URL, and never logged.
 *
 * Every failure renders the SAME message. The backend already refuses to
 * distinguish a wrong passcode from a rate-limited address; echoing a more
 * specific reason here would undo that. The one exception is the lockout, which
 * a patient must be told about because the remedy is to wait rather than retry.
 */

const GENERIC_FAILURE = "That passcode was not accepted.";
const LOCKED_OUT =
  "Too many attempts have been made from this location. Please wait a few minutes and try again.";

interface AccessFormProps {
  /** Where to go after signing in. Validated on the server; relative only. */
  next?: string | undefined;
}

export function AccessForm({ next }: AccessFormProps) {
  const router = useRouter();
  const [passcode, setPasscode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setError(null);
    try {
      const response = await fetch("/api/access/login", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ passcode, next }),
      });
      if (response.ok) {
        const body: unknown = await response.json();
        const destination =
          typeof body === "object" && body !== null && "next" in body
            ? String((body as { next: unknown }).next)
            : "/";
        // `replace`, not `push`: the passcode screen must not be reachable with
        // the browser's back button once a session exists.
        router.replace(destination as "/");
        router.refresh();
        return;
      }
      setError(response.status === 429 ? LOCKED_OUT : GENERIC_FAILURE);
    } catch {
      setError(GENERIC_FAILURE);
    } finally {
      setPending(false);
      setPasscode("");
    }
  }

  return (
    <form onSubmit={onSubmit} noValidate>
      <label htmlFor="passcode" className="block text-sm font-medium text-display">
        Access passcode
      </label>
      <p id="passcode-help" className="mt-1 text-[13px] text-ink-secondary">
        Provided by whoever set up this demonstration.
      </p>
      <input
        id="passcode"
        name="passcode"
        type="password"
        required
        autoComplete="off"
        autoCapitalize="none"
        spellCheck={false}
        value={passcode}
        onChange={(event) => setPasscode(event.target.value)}
        aria-describedby="passcode-help"
        aria-invalid={error !== null}
        {...(error ? { "aria-errormessage": "passcode-error" } : {})}
        className="mt-3 block min-h-[52px] w-full rounded-[14px] border border-border bg-surface px-4 text-[17px] text-ink shadow-[0_1px_2px_rgb(26_21_35_/_0.04)] outline-none transition-colors focus:border-accent"
      />

      {/*
        A live region that EXISTS before it has content. A region created at the
        moment its text arrives is often not announced — the reason the previous
        interface's announcements were unreliable (accessibility.md §6, #4).
      */}
      <p
        id="passcode-error"
        role="status"
        aria-live="polite"
        className={`mt-3 text-sm ${error ? "text-emergency" : "sr-only"}`}
      >
        {error ?? ""}
      </p>

      <button
        type="submit"
        disabled={pending || passcode.length === 0}
        className="mt-5 inline-flex min-h-[52px] w-full items-center justify-center rounded-[14px] bg-accent-royal px-5 text-[16px] font-medium text-white transition-colors hover:bg-[#8B5CF6] disabled:cursor-not-allowed disabled:opacity-40"
      >
        {pending ? "Checking…" : "Continue"}
      </button>
    </form>
  );
}
