import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

/*
 * Guarded because this setup file is shared with `@vitest-environment node`
 * specs — the session and proxy modules run on the server, never in a browser,
 * and there is no `window` there to patch.
 */
if (typeof window !== "undefined") {
  // jsdom implements no media queries. Components that branch on
  // `prefers-reduced-motion` need one, and the default here is "no preference"
  // so a test asserting the reduced-motion path has to ask for it explicitly.
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  });
}
