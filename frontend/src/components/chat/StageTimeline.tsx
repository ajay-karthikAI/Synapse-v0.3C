"use client";

import type { StageEvent } from "@/lib/turns";

/**
 * What the system is doing, while it does it.
 *
 * Both fields of a stage come from the server's frozen table in
 * `synapse.service.progress` — a closed enum and a fixed message, with no
 * parameter through which a query or an answer could reach the wire. So this
 * component can render them directly: it is structurally impossible for medical
 * content to arrive here. Nothing is streamed token by token, and that is the
 * point. Showing unverified prose and retracting it later is precisely what the
 * answer layer exists to prevent, so the page shows *progress* until the turn
 * is complete and then shows the whole validated result at once.
 *
 * **Announcement.** The container is `aria-live="polite"` and `aria-atomic` is
 * off, so a screen reader reads each new stage as it is appended rather than
 * re-reading the list. Only the newest line is exposed; the completed ones are
 * `aria-hidden` and remain visible for sighted users as a sense of pace.
 *
 * **Heartbeats.** The backend sends an SSE comment every few seconds during a
 * long turn to stop a proxy closing the connection. It carries no content, so
 * it changes no text — it only advances the "still working" indicator, which is
 * how a patient can tell a slow turn from a dead one.
 */

interface StageTimelineProps {
  stages: readonly StageEvent[];
  /** Increments on every keep-alive. Only its changing matters, not its value. */
  heartbeats: number;
}

export function StageTimeline({ stages, heartbeats }: StageTimelineProps) {
  const current = stages.at(-1);
  const earlier = stages.slice(0, -1);

  return (
    <div className="mx-auto mt-10 max-w-xl rounded-[16px] border border-rule bg-surface px-5 py-4">
      <ol className="space-y-1.5">
        {earlier.map((stage) => (
          <li
            key={stage.stage}
            aria-hidden="true"
            className="flex items-center gap-2.5 text-[13px] text-ink-secondary/70"
          >
            <span className="text-accent-soft">✓</span>
            {stage.message}
          </li>
        ))}
      </ol>

      {/* The live region exists before it has content: one created at the
          moment its text arrives is often not announced at all. */}
      <p
        role="status"
        aria-live="polite"
        className="mt-1.5 flex items-center gap-2.5 text-[14px] text-ink"
      >
        {current ? (
          <>
            <span aria-hidden="true" className="stage-pulse text-accent">
              ●
            </span>
            {current.message}
          </>
        ) : (
          "Working on your question..."
        )}
      </p>

      {/*
        Heartbeat evidence. Rendered as text for a sighted reader and hidden
        from the live region — a connection keep-alive is not something to
        announce every few seconds, but a silent screen with no sign of life is
        how someone concludes the page has hung and reloads mid-turn.
      */}
      {heartbeats > 0 ? (
        <p aria-hidden="true" className="mt-2 text-[11px] text-ink-secondary/70">
          Still working — this can take up to two minutes.
        </p>
      ) : null}
    </div>
  );
}
