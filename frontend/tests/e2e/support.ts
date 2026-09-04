import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import type { BrowserContext, Page } from "@playwright/test";
import { SignJWT } from "jose";

/**
 * Shared end-to-end scaffolding: a signed access cookie, and a stubbed backend.
 *
 * The backend is never running. Every call the page makes to `/api/proxy/**` is
 * answered from a **golden fixture** — the same JSON the Python suite generates
 * from `tests/golden_states.py` — so these tests exercise the real composer, the
 * real SSE parser, the real focus management and the real CSP against payloads
 * that are by construction the ones the server sends.
 *
 * Lives here rather than in one spec file because three suites need it:
 * behaviour (`conversation.spec.ts`), appearance (`visual.spec.ts`) and
 * accessibility (`a11y.spec.ts`). A second copy would drift, and the copy that
 * drifted would be the one still asserting the old contract.
 */

/** Matches `playwright.config.ts`'s webServer env. Not a secret: no real deployment uses it. */
export const JWT_SECRET = "e2e-jwt-secret-0123456789abcdef01";

const FIXTURES = join(process.cwd(), "tests", "fixtures");

export function fixture(name: string): unknown {
  return JSON.parse(readFileSync(join(FIXTURES, `${name}.json`), "utf8"));
}

/** Every golden envelope state name, discovered from the fixture directory. */
export function everyEnvelopeState(): string[] {
  return readdirSync(FIXTURES)
    .filter((file) => file.startsWith("envelope.") && file.endsWith(".json"))
    .map((file) => file.slice("envelope.".length, -".json".length))
    .sort();
}

export async function signIn(context: BrowserContext) {
  const token = await new SignJWT({ sid: "e2e-session-0123456789ab" })
    .setProtectedHeader({ alg: "HS256" })
    .setIssuedAt()
    .setExpirationTime("8h")
    .sign(new TextEncoder().encode(JWT_SECRET));
  await context.addCookies([
    {
      name: "synapse_access",
      value: token,
      domain: "127.0.0.1",
      path: "/",
      httpOnly: true,
      sameSite: "Lax",
    },
  ]);
}

export function sse(...frames: [string, unknown][]): string {
  return frames
    .map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)
    .join("");
}

export interface StubOptions {
  /** The golden state the turn resolves to. */
  state?: string;
  /** Replaces the whole SSE body, for stage and heartbeat cases. */
  body?: string;
  /** Status for the turn request, to exercise transport failures. */
  status?: number;
  turnCount?: number;
  /**
   * Hold the turn open instead of answering, so the loading state can be
   * captured. The returned promise never resolves; the test ends first.
   */
  hang?: boolean;
}

/** Answer every proxy call the page makes. */
export async function stubBackend(page: Page, options: StubOptions = {}) {
  const { state = "answer", status = 200, turnCount = 0 } = options;

  await page.route("**/api/proxy/v1/session", async (route) => {
    if (route.request().method() === "DELETE") {
      return route.fulfill({ json: { deleted: true } });
    }
    return route.fulfill({
      json: {
        session_id: "e2e",
        turn_count: turnCount,
        max_turns: 20,
        turn_active: false,
        idle_ttl_seconds: 7200,
      },
    });
  });

  await page.route("**/api/proxy/v1/turns/stream", async (route) => {
    if (status !== 200) {
      return route.fulfill({ status, json: { code: "unauthorized", message: "no" } });
    }
    if (options.hang) {
      // Never fulfilled: the composer stays busy and the stage timeline shows.
      return new Promise<void>(() => {});
    }
    return route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: options.body ?? sse(["envelope", fixture(`envelope.${state}`)]),
    });
  });

  await page.route("**/api/proxy/v1/transparency", async (route) =>
    route.fulfill({ json: fixture("transparency") }),
  );

  await page.route("**/api/proxy/v1/turns/*/brief", async (route) =>
    route.fulfill({ json: fixture("brief") }),
  );
  await page.route("**/api/proxy/v1/turns/*/brief/**", async (route) => {
    if (route.request().url().includes("/export/")) {
      return route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/plain; charset=utf-8",
          "content-disposition": 'attachment; filename="appointment-brief-e2e.txt"',
          "x-content-type-options": "nosniff",
        },
        body: "APPOINTMENT BRIEF\n",
      });
    }
    return route.fulfill({ json: fixture("brief") });
  });
}

export async function ask(page: Page, question = "what does my HbA1c mean?") {
  await page.getByLabel("Your question or symptoms").fill(question);
  await page.getByRole("button", { name: "Ask", exact: true }).click();
}

/**
 * Wait until the page has stopped moving, then pin it to the top.
 *
 * `toHaveScreenshot({ animations: "disabled" })` freezes CSS animations and
 * transitions, but a turn completing calls
 * `scrollIntoView({ behavior: "smooth" })`, which is a script-driven scroll that
 * Playwright does not know to wait for. Combined with the sticky site header,
 * that produced a genuinely flaky baseline: the header rendered at whatever
 * offset the scroll happened to have reached.
 *
 * So: force scrolling to be instant, poll until the offset holds still across
 * several frames, and return to the top so every full-page capture starts from
 * the same place.
 */
export async function settle(page: Page) {
  await page.addStyleTag({
    content: "html, *, *::before, *::after { scroll-behavior: auto !important; }",
  });
  await page.evaluate(
    () =>
      new Promise<void>((resolve) => {
        let previous = Number.NaN;
        let steady = 0;
        const tick = () => {
          if (window.scrollY === previous) {
            steady += 1;
            if (steady >= 3) {
              resolve();
              return;
            }
          } else {
            steady = 0;
            previous = window.scrollY;
          }
          requestAnimationFrame(tick);
        };
        requestAnimationFrame(tick);
      }),
  );
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.waitForTimeout(120);
}
