"use client";

import { useCallback, useSyncExternalStore } from "react";

/**
 * Track a media query from React.
 *
 * Used where a breakpoint changes *semantics* rather than only appearance. The
 * appointment brief is the case: on a wide screen it is a side panel beside the
 * answer, which does not trap focus and does not make the rest of the page
 * inert; on a narrow screen the same panel covers the whole viewport, which
 * makes it a modal dialog and obliges it to trap focus and be dismissible.
 * Those are different ARIA roles and different keyboard contracts, so CSS alone
 * cannot express the difference.
 *
 * `useSyncExternalStore` rather than `useState` in an effect. A media query is
 * exactly what that hook is for — an external source of truth React does not
 * own — and it avoids the cascading render that setting state during an effect
 * causes. It also takes a separate server snapshot, which is what makes the
 * server and the first client render agree: both start narrow, so there is no
 * hydration mismatch and no flash of the wrong layout.
 */
export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback(
    (onChange: () => void) => {
      // Guarded: jsdom without `matchMedia`, and any renderer that lacks it,
      // gets the narrow layout rather than a crash.
      if (typeof window === "undefined" || !window.matchMedia) return () => {};
      const list = window.matchMedia(query);
      list.addEventListener("change", onChange);
      return () => list.removeEventListener("change", onChange);
    },
    [query],
  );

  const getSnapshot = useCallback(() => {
    if (typeof window === "undefined" || !window.matchMedia) return false;
    return window.matchMedia(query).matches;
  }, [query]);

  // The server has no viewport. Narrow is the honest default: it is the layout
  // that assumes least, and the modal semantics it implies are the safer of the
  // two to render before the real width is known.
  const getServerSnapshot = useCallback(() => false, []);

  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}

/** The width at which the brief becomes a side panel rather than a sheet. */
export const DESKTOP_QUERY = "(min-width: 1024px)";
