import { expect, test } from "@playwright/test";

import { ask, everyEnvelopeState, fixture, settle, signIn, stubBackend } from "./support";

/**
 * Visual snapshots, across the widths the interface actually has to survive.
 *
 * **320** is the narrowest width WCAG 1.4.10 requires content to reflow into,
 * and is where a source list or a brief editor breaks first. **375** is the
 * common phone. **768** is the tablet boundary, and the width at which the
 * appointment brief changes from a modal dialog to a side region — a semantic
 * change, not a cosmetic one, so both sides of it are captured. **1440** is the
 * desktop the design was drawn at.
 *
 * Both projects run these, and each keeps its own baselines — 64 named
 * `-chromium-linux` and 64 `-mobile-linux`. That is not a second copy of the
 * same picture: the mobile project carries a touch-capable, mobile user-agent
 * context, and the appointment brief is a modal `dialog` there against a
 * non-modal `complementary` region on desktop. The widths below are applied on
 * top of that context, so the pair captures the same width under both
 * contracts.
 *
 * Determinism
 * -----------
 * Animations are disabled and the caret is hidden, so a snapshot cannot depend
 * on when the screenshot happened to be taken. Fonts are self-hosted by
 * `next/font`, so no network race can change metrics. Every payload comes from
 * a committed golden fixture. A diff here is therefore a real change to what a
 * patient sees — which is the only reason to keep image baselines at all.
 */

const WIDTHS = [320, 375, 768, 1440] as const;

/**
 * Which golden states get an image baseline.
 *
 * Every state whose screen is structurally distinct — answer, partial support,
 * abstention, medical-staff referral, rewritten follow-up, and both emergencies
 * — plus ONE failure. The other failures are the same screen by contract: a
 * failure carries fixed application copy and a typed code, never a
 * per-code message, so seven baselines would be seven copies of one image and a
 * wording change would have to be caught seven times.
 *
 * That contract is not assumed here. `every failure renders the identical
 * fixed copy` below asserts it against every remaining failure fixture, so if a
 * code ever grows its own screen this test fails rather than the difference
 * going unphotographed.
 */
const FAILURE_BASELINE = "failure_internal_error";

function snapshotStates(): string[] {
  return everyEnvelopeState().filter((state) => {
    const kind = (fixture(`envelope.${state}`) as { kind: string }).kind;
    return kind !== "failure" || state === FAILURE_BASELINE;
  });
}

function otherFailureStates(): string[] {
  return everyEnvelopeState().filter((state) => {
    const kind = (fixture(`envelope.${state}`) as { kind: string }).kind;
    return kind === "failure" && state !== FAILURE_BASELINE;
  });
}

/** Tall enough that a full answer is one image rather than a scroll position. */
const HEIGHT = 900;

const shot = { animations: "disabled", caret: "hide", fullPage: true } as const;

test.describe("visual", () => {
  // Baselines are per platform, so
  // that CI gates on the same images a developer regenerates. On any other
  // platform these would compare Linux baselines against differently
  // rasterised text and fail on every glyph. Run them anywhere with
  // `scripts/visual_snapshots.sh`, which uses the pinned Playwright container.
  test.skip(
    process.platform !== "linux",
    "visual baselines are Linux-only: run scripts/visual_snapshots.sh",
  );

  test.beforeEach(async ({ context }) => {
    await signIn(context);
  });

  for (const width of WIDTHS) {
    test.describe(`${width}px`, () => {
      test.use({ viewport: { width, height: HEIGHT } });

      test("access", async ({ page, context }) => {
        // Signed out: clear the cookie this file's beforeEach installed.
        await context.clearCookies();
        await page.goto("/access");
        await expect(page.getByRole("main")).toBeVisible();
        await settle(page);
        await expect(page).toHaveScreenshot(`access-${width}.png`, shot);
      });

      test("empty chat", async ({ page }) => {
        await stubBackend(page);
        await page.goto("/");
        await expect(page.getByLabel("Your question or symptoms")).toBeVisible();
        await settle(page);
        await expect(page).toHaveScreenshot(`empty-chat-${width}.png`, shot);
      });

      test("loading", async ({ page }) => {
        // The turn never resolves, so the stage timeline is the whole screen.
        await stubBackend(page, { hang: true });
        await page.goto("/");
        await ask(page);
        await expect(page.getByRole("button", { name: /working/i })).toBeVisible();
        await settle(page);
        await expect(page).toHaveScreenshot(`loading-${width}.png`, shot);
      });

      test("transparency", async ({ page }) => {
        await stubBackend(page);
        await page.goto("/transparency");
        await expect(page.getByRole("main")).toBeVisible();
        await settle(page);
        await expect(page).toHaveScreenshot(`transparency-${width}.png`, shot);
      });

      // Discovered from the fixture directory rather than listed here — so a
      // state added on the Python side gets a baseline instead of quietly
      // never being looked at.
      for (const state of snapshotStates()) {
        test(`turn: ${state}`, async ({ page }) => {
          await stubBackend(page, { state });
          await page.goto("/");
          await ask(page);
          await page.waitForSelector("[id$='-result']");
          await settle(page);
          await expect(page).toHaveScreenshot(`turn-${state}-${width}.png`, shot);
        });
      }

      test("evidence open", async ({ page }) => {
        await stubBackend(page);
        await page.goto("/");
        await ask(page);
        await page.getByText(/passages this came from/i).click();
        // A native <details>, so the state is the `open` attribute rather than
        // aria-expanded — the element manages its own semantics.
        await expect(page.locator("details[open]")).toHaveCount(1);
        await expect(page.getByText(/passages this came from/i)).toBeVisible();
        await settle(page);
        await expect(page).toHaveScreenshot(`evidence-open-${width}.png`, shot);
      });

      test("source panel", async ({ page }) => {
        await stubBackend(page);
        await page.goto("/");
        await ask(page);
        const sources = page.getByRole("heading", { name: "Sources" });
        await sources.scrollIntoViewIfNeeded();
        await expect(sources).toBeVisible();
        // The sources region alone: a full-page shot at 1440 makes a one-line
        // wording change in a citation invisible in the diff.
        await settle(page);
        await expect(page.locator("section", { has: sources }).first()).toHaveScreenshot(
          `source-panel-${width}.png`,
          { animations: "disabled", caret: "hide" },
        );
      });

      test("brief editor", async ({ page }) => {
        await stubBackend(page);
        await page.goto("/");
        await ask(page);
        await page.getByRole("button", { name: /Build an appointment brief/ }).click();
        await page.getByRole("heading", { name: /Your appointment brief/ }).waitFor();
        await settle(page);
        // VIEWPORT, not full page. Below 1024 the brief is a `position: fixed`
        // modal, and a full-page capture paints a fixed element over only the
        // first viewport height — the panel came out clipped with the page
        // bleeding through beneath it. That baseline would have been a picture
        // of a screenshot artefact rather than of the interface.
        await expect(page).toHaveScreenshot(`brief-editor-${width}.png`, {
          animations: "disabled",
          caret: "hide",
          fullPage: false,
        });
      });
    });
  }

  /**
   * The assumption the failure baseline rests on, checked rather than trusted.
   *
   * If a failure code ever grows its own screen, this fails — and the right
   * response is to give that state a baseline, not to relax the assertion.
   */
  test("every failure renders the identical fixed copy, differing only in its typed code", async ({
    page,
  }) => {
    const REFERENCE = /Reference code: ([a-z_]+)/;
    const rendered: Record<string, { copy: string; code: string }> = {};

    for (const state of [FAILURE_BASELINE, ...otherFailureStates()]) {
      await stubBackend(page, { state });
      await page.goto("/");
      await ask(page);
      await page.waitForSelector("[id$='-result']");
      const text = (await page.locator("main").innerText()).replace(/\s+/g, " ").trim();
      rendered[state] = {
        // The code is the ONE thing permitted to vary, so it is removed before
        // comparing and asserted separately below.
        copy: text.replace(REFERENCE, "Reference code: <code>"),
        code: text.match(REFERENCE)?.[1] ?? "",
      };
    }

    const baseline = rendered[FAILURE_BASELINE]!;
    for (const [state, { copy }] of Object.entries(rendered)) {
      expect(copy, `${state} shows different copy from ${FAILURE_BASELINE}`).toBe(baseline.copy);
    }

    // "typed codes only": every failure carries one, and no two share it.
    //
    // This assertion had a named exception until Phase 6. `index_unverified`
    // could not reach the turn path, so an unverifiable index and a retrieval
    // crash both rendered `retrieval_failed` and an operator could not tell a
    // corrupted index from a transient fault. The pipeline now preserves that
    // code, `failure_index_gate_raised` renders as an insufficient-evidence
    // state naming the reason, and uniqueness holds without exception.
    const codes = Object.entries(rendered).map(([state, entry]) => [state, entry.code] as const);
    expect(codes.every(([, code]) => code.length > 0)).toBe(true);
    expect(
      new Set(codes.map(([, code]) => code)).size,
      `two faults share a reference code: ${JSON.stringify(codes)}`,
    ).toBe(codes.length);

    // Guards against the whole assertion passing on an empty screen.
    expect(baseline.copy.length).toBeGreaterThan(80);
  });
});
