"use client";

import { useId } from "react";

/**
 * The Synapse mark: an ECG trace on a transparent ground, drawn once on mount.
 *
 * The geometry is the original identity's, point for point. It is not
 * re-derived, re-tuned, or "cleaned up" — the mark is the one thing a patient
 * recognises across the Streamlit interface and this one, and a mark that
 * drifts is a different mark.
 *
 * Three things make it read rather than sit there:
 *
 * 1. **The gradient runs along the trace**, deep indigo into violet into white,
 *    with the white confined to the last 6%. The eye lands on the bright
 *    endpoint, which is where the trace finishes drawing.
 * 2. **A layered glow, and a hotter bloom over the final stretch.** A wide blur
 *    for the halo and a tight one for the core, merged under the unblurred
 *    source so the line itself stays crisp — one blur alone reads as a smudge,
 *    several read as light. The bright tail is a SECOND stroke of the same
 *    polyline, transparent until 88% and white by 94%, so it is a continuation
 *    of the trace rather than a mark placed at its end.
 * 3. **`pathLength="600"`** normalises the geometry so the dash animation is
 *    expressed in round numbers instead of the polyline's real length. Change
 *    the points and the animation still runs correctly.
 *
 * The trace draws once and stops — `forwards`, iteration count exactly 1, no
 * loop. A logo that keeps redrawing is a distraction in an interface someone
 * uses while anxious.
 *
 * IDs are per-instance via `useId`. Two marks on one page with a shared
 * gradient id would have the second silently adopt the first's paint. Colons
 * are stripped because React's generated ids contain them and a colon is not
 * valid in a CSS-addressable id.
 *
 * `aria-hidden` throughout: the mark duplicates the wordmark beside it, and
 * announcing "image" before every heading is noise.
 */

export type BrandMarkSize = "compact" | "hero";

interface BrandMarkProps {
  size?: BrandMarkSize;
  /** Set false on repeat appearances so the trace is not redrawn. */
  animate?: boolean;
  className?: string;
}

/** Box and corner radius per size. */
const DIMENSIONS: Record<BrandMarkSize, { box: number; radius: number }> = {
  compact: { box: 44, radius: 12 },
  hero: { box: 210, radius: 16 },
};

/**
 * The original trace: flat baseline, a short lead-in, the spike up-down, then
 * flat again. Unchanged from the identity it came from.
 */
const TRACE_POINTS = "8,100 65,100 78,100 92,36 108,164 122,100 138,100 192,100";


export function BrandMark({ size = "compact", animate = true, className }: BrandMarkProps) {
  // React ids contain colons, which are not valid in a CSS identifier and which
  // some SVG tooling mishandles in url() references.
  const uid = useId().replace(/:/g, "");
  const gradientId = `brandmark-stroke-${uid}`;
  const filterId = `brandmark-glow-${uid}`;
  const highlightId = `brandmark-highlight-${uid}`;
  const bloomFilterId = `brandmark-bloom-${uid}`;

  const { box, radius } = DIMENSIONS[size];

  return (
    <span
      aria-hidden="true"
      className={className}
      style={{
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        width: box,
        height: box,
        borderRadius: radius,
        // Transparent: the mark sits directly on whatever ground it is placed
        // on, so the glow bleeds into the page instead of stopping at a tile
        // edge. `overflow: visible` for the same reason — a clipped glow has a
        // hard border exactly where it should be softest.
        backgroundColor: "transparent",
        overflow: "visible",
        flexShrink: 0,
      }}
    >
      <svg
        width={box}
        height={box}
        viewBox="0 0 200 200"
        role="presentation"
        aria-hidden="true"
        focusable="false"
      >
        <defs>
          {/*
           * Horizontal, so the ramp follows the trace left to right. The white
           * is pinned to 94% and 100%: any earlier and the whole right-hand
           * baseline washes out, losing the "the pen just passed here" read
           * that makes the endpoint the brightest thing in the mark.
           */}
          <linearGradient id={gradientId} x1="0%" y1="0%" x2="100%" y2="0%">
            <stop offset="0%" stopColor="#39248F" />
            <stop offset="36%" stopColor="#5B3FD6" />
            <stop offset="90%" stopColor="#8B78E8" />
            <stop offset="94%" stopColor="#FFFFFF" />
            <stop offset="100%" stopColor="#FFFFFF" />
          </linearGradient>

          {/*
           * Generous bounds: a blur clipped by its own filter region produces a
           * hard edge exactly where the glow should be softest. sRGB rather
           * than the linearRGB default, so the blur matches what the gradient
           * stops were picked against.
           */}
          <filter
            id={filterId}
            x="-24%"
            y="-44%"
            width="148%"
            height="188%"
            colorInterpolationFilters="sRGB"
          >
            {/*
             * Three layers rather than two. The widest is doubled up via
             * feMerge so the halo carries real weight without raising
             * stdDeviation to the point where the line itself goes soft.
             */}
            <feGaussianBlur in="SourceGraphic" stdDeviation="4" result="glowWide" />
            <feGaussianBlur in="SourceGraphic" stdDeviation="1.4" result="glowTight" />
            <feMerge>
              {/* Widest first, then tighter, then the crisp line on top. */}
              <feMergeNode in="glowWide" />
              <feMergeNode in="glowWide" />
              <feMergeNode in="glowTight" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>

          {/*
           * A gradient that is invisible along most of the trace and turns
           * white over the final stretch. Painted onto a SECOND copy of the
           * same polyline, so the bright tail is literally the same line —
           * same points, same width, same caps — rather than an object sitting
           * on top of it.
           */}
          <linearGradient id={highlightId} x1="0%" y1="0%" x2="100%" y2="0%">
            <stop offset="0%" stopColor="#FFFFFF" stopOpacity="0" />
            <stop offset="88%" stopColor="#FFFFFF" stopOpacity="0" />
            <stop offset="94%" stopColor="#FFFFFF" stopOpacity="1" />
            <stop offset="100%" stopColor="#FFFFFF" stopOpacity="1" />
          </linearGradient>

          {/*
           * A wider bloom for that tail. Because it is applied to a stroke
           * rather than a shape, the glow follows the line's own path — which
           * is what makes the end read as the hot tip of a trace instead of a
           * lamp placed at its end.
           */}
          <filter
            id={bloomFilterId}
            x="-300%"
            y="-300%"
            width="700%"
            height="700%"
            colorInterpolationFilters="sRGB"
          >
            <feGaussianBlur in="SourceGraphic" stdDeviation="7" result="bloomWide" />
            <feGaussianBlur in="SourceGraphic" stdDeviation="2.6" result="bloomTight" />
            <feMerge>
              <feMergeNode in="bloomWide" />
              <feMergeNode in="bloomWide" />
              <feMergeNode in="bloomTight" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
        </defs>

        <polyline
          className={animate ? "brandmark-trace" : undefined}
          points={TRACE_POINTS}
          fill="none"
          stroke={`url(#${gradientId})`}
          strokeWidth="4"
          strokeLinecap="round"
          strokeLinejoin="round"
          // Normalises the geometry to 600 units so the dash animation reads in
          // round numbers regardless of the polyline's true length.
          pathLength="600"
          filter={`url(#${filterId})`}
        />

        {/*
         * The bright tail: the same polyline again, painted with the highlight
         * gradient and bloomed. It carries the same draw animation, so it
         * arrives exactly as the trace reaches the end rather than fading in
         * afterwards.
         */}
        <polyline
          className={animate ? "brandmark-trace" : undefined}
          points={TRACE_POINTS}
          fill="none"
          stroke={`url(#${highlightId})`}
          strokeWidth="4"
          strokeLinecap="round"
          strokeLinejoin="round"
          pathLength="600"
          filter={`url(#${bloomFilterId})`}
        />
      </svg>
    </span>
  );
}
