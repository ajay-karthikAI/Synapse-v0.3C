import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";
import { expect, test } from "@playwright/test";

import { ask, everyEnvelopeState, settle, signIn, stubBackend } from "./support";

/**
 * Accessibility beyond what a scanner can see.
 *
 * axe finds contrast, names and roles. It cannot tell you whether the interface
 * survives being magnified, whether someone who never touches a mouse can
 * actually complete a turn, whether focus is visible while they do it, or
 * whether the page makes a noise. Those are asserted here, against the real
 * production build.
 *
 * Exceptions policy
 * -----------------
 * There are no allowed axe exceptions in this file. Any that are ever added
 * must be justified by the finding being demonstrably outside Synapse-owned
 * markup — the one known example being Next's own injected route announcer,
 * which is why every assertion here is scoped to `main` rather than to the
 * document.
 */

const AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

const scan = (page: Page) =>
  // Scoped to the application's own markup. Next injects an empty
  // `role="alert"` route announcer into <body> that the application does not
  // own and cannot remove.
  new AxeBuilder({ page }).include("main").withTags(AXE_TAGS).analyze();

test.beforeEach(async ({ context }) => {
  await signIn(context);
});

test.describe("axe, WCAG 2.2 AA", () => {
  // Every state the server can produce, discovered from the fixture directory.
  for (const state of everyEnvelopeState()) {
    test(`${state} has no violations`, async ({ page }) => {
      await stubBackend(page, { state });
      await page.goto("/");
      await ask(page);
      await page.waitForSelector("[id$='-result']");

      const results = await scan(page);
      expect(results.violations).toEqual([]);
    });
  }

  test("the brief editor has no violations", async ({ page }) => {
    await stubBackend(page);
    await page.goto("/");
    await ask(page);
    await page.getByRole("button", { name: /Build an appointment brief/ }).click();
    await page.getByRole("heading", { name: /Your appointment brief/ }).waitFor();

    const results = await scan(page);
    expect(results.violations).toEqual([]);
  });

  test("the opened evidence disclosure has no violations", async ({ page }) => {
    await stubBackend(page);
    await page.goto("/");
    await ask(page);
    await page.getByText(/passages this came from/i).click();
    await expect(page.locator("details[open]")).toHaveCount(1);

    const results = await scan(page);
    expect(results.violations).toEqual([]);
  });
});

test.describe("magnification", () => {
  /**
   * WCAG 1.4.4 (Resize Text) at 200%, and 1.4.10 (Reflow) at 400%.
   *
   * Browser zoom multiplies CSS pixel size, so a 1280px window at 200% zoom
   * presents a 640px CSS viewport. Emulating it that way — rather than by
   * setting a CSS `zoom` property — is what the user actually experiences, and
   * it is what catches a layout pinned to a pixel width.
   *
   * The assertion is the one that matters: content must reflow rather than
   * require scrolling in two directions at once.
   */
  const noHorizontalScroll = async (page: Page) => {
    await settle(page);
    return page.evaluate(() => {
      const doc = document.documentElement;
      // 1px of tolerance for sub-pixel layout rounding.
      return doc.scrollWidth - doc.clientWidth <= 1;
    });
  };

  for (const [label, size] of [
    ["200% of 1280", { width: 640, height: 512 }],
    ["400% of 1280", { width: 320, height: 256 }],
  ] as const) {
    test(`an answer reflows at ${label} without horizontal scrolling`, async ({ page }) => {
      await page.setViewportSize(size);
      await stubBackend(page);
      await page.goto("/");
      await ask(page);
      await page.waitForSelector("[id$='-result']");

      expect(await noHorizontalScroll(page)).toBe(true);
      // Reflow must not cost content: the citation and its source survive.
      await expect(page.getByRole("heading", { name: "Sources" })).toBeVisible();
      await expect(page.getByRole("link", { name: /HbA1c Targets in Adults/ })).toBeVisible();
    });

    test(`the brief editor reflows at ${label}`, async ({ page }) => {
      await page.setViewportSize(size);
      await stubBackend(page);
      await page.goto("/");
      await ask(page);
      await page.getByRole("button", { name: /Build an appointment brief/ }).click();
      await page.getByRole("heading", { name: /Your appointment brief/ }).waitFor();

      expect(await noHorizontalScroll(page)).toBe(true);
    });
  }
});

test.describe("keyboard only", () => {
  test("a whole turn can be completed without a pointer", async ({ page }) => {
    await stubBackend(page);
    await page.goto("/");

    // Reach the field by tabbing, not by clicking it.
    const field = page.getByLabel("Your question or symptoms");
    let reached = false;
    for (let press = 0; press < 25 && !reached; press += 1) {
      await page.keyboard.press("Tab");
      reached = await field.evaluate((node) => node === document.activeElement);
    }
    expect(reached, "the question field is not reachable by Tab").toBe(true);

    // Enter submits; Shift+Enter would newline. Typed, not filled.
    await page.keyboard.type("what does my HbA1c mean?");
    await page.keyboard.press("Enter");

    await page.waitForSelector("[id$='-result']");
    await expect(page.getByRole("heading", { name: "What the research says" })).toBeFocused();
  });

  test("the evidence disclosure opens from the keyboard", async ({ page }) => {
    await stubBackend(page);
    await page.goto("/");
    await ask(page);
    await page.waitForSelector("[id$='-result']");

    const summary = page.getByText(/passages this came from/i);
    await summary.focus();
    await page.keyboard.press("Enter");
    await expect(page.locator("details[open]")).toHaveCount(1);
  });

  test("the brief closes on Escape and returns focus to its trigger", async ({ page }) => {
    await stubBackend(page);
    await page.goto("/");
    await ask(page);

    const trigger = page.getByRole("button", { name: /Build an appointment brief/ });
    await trigger.click();
    await page.getByRole("heading", { name: /Your appointment brief/ }).waitFor();

    await page.keyboard.press("Escape");
    // Focus must land somewhere usable, not on <body>: the disclosure button it
    // came from is what the reader was last on.
    await expect(page.getByRole("button", { name: /Build an appointment brief/ })).toBeFocused();
  });

  test("no element is reachable only by pointer", async ({ page }) => {
    await stubBackend(page);
    await page.goto("/");
    await ask(page);
    await page.waitForSelector("[id$='-result']");

    // Every interactive element inside main must be focusable. A negative
    // tabindex on a button is the usual way this breaks.
    const unreachable = await page.locator("main").evaluate((main) => {
      const interactive = main.querySelectorAll<HTMLElement>(
        "a[href], button, input, textarea, select, summary, [role='button']",
      );
      return Array.from(interactive)
        .filter((node) => {
          if (node.hasAttribute("disabled")) return false;
          if (node.getAttribute("aria-hidden") === "true") return false;
          const index = node.getAttribute("tabindex");
          return index !== null && Number(index) < 0;
        })
        .map((node) => `${node.tagName}:${(node.textContent ?? "").slice(0, 40)}`);
    });
    expect(unreachable).toEqual([]);
  });
});

test.describe("visible focus", () => {
  /**
   * WCAG 2.4.7, driven by the actual Tab key.
   *
   * The indicator is a `:focus-visible` rule, and `:focus-visible` is a
   * heuristic the browser applies to *keyboard* focus — a programmatic
   * `element.focus()` does not match it. An earlier version of this test called
   * `.focus()` in a loop, saw no computed-style change on any element, and
   * reported every control in the answer as having no focus ring. The interface
   * was fine; the test was measuring the wrong thing.
   *
   * So focus is moved the way a keyboard user moves it, and the assertion is
   * the requirement itself: while focused, the element paints an outline or a
   * shadow.
   */
  test("every interactive element in an answer shows a focus indicator", async ({ page }) => {
    await stubBackend(page);
    await page.goto("/");
    await ask(page);
    await page.waitForSelector("[id$='-result']");
    await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());

    const seen: string[] = [];
    const invisible: string[] = [];

    for (let stop = 0; stop < 40; stop += 1) {
      await page.keyboard.press("Tab");
      const result = await page.evaluate(() => {
        const node = document.activeElement as HTMLElement | null;
        if (!node || node === document.body) return null;
        if (!node.closest("main")) return { inMain: false } as const;

        // The indicator is not always on the focused element. The composer
        // paints it on the wrapping container with `focus-within:border-accent`
        // — a perfectly good visible indicator that an element-only check
        // reports as missing. So the element and three ancestors are compared.
        const chain: HTMLElement[] = [];
        for (let el: HTMLElement | null = node; el && chain.length < 4; el = el.parentElement) {
          chain.push(el);
        }
        const signature = () =>
          chain
            .map((el) => {
              const s = getComputedStyle(el);
              return [
                s.outlineStyle,
                s.outlineWidth,
                s.outlineColor,
                s.boxShadow,
                s.borderColor,
                s.backgroundColor,
              ].join("|");
            })
            .join("~");

        const focused = signature();
        node.blur();
        const blurred = signature();
        // Restore so the next Tab continues from here rather than the top.
        node.focus();

        return {
          inMain: true,
          visible: focused !== blurred,
          label: `${node.tagName}: ${(node.textContent ?? "").trim().slice(0, 40)}`,
        } as const;
      });

      if (!result || !result.inMain) continue;
      seen.push(result.label);
      if (!result.visible) invisible.push(result.label);
    }

    // Guards against the loop tabbing straight out of the page and asserting
    // nothing at all.
    expect(seen.length, "no focusable element inside main was reached").toBeGreaterThan(4);
    expect(invisible, "these show no focus indicator while keyboard-focused").toEqual([]);
  });
});

test.describe("no motion, no sound", () => {
  test("nothing on the page can make a noise", async ({ page }) => {
    // Installed BEFORE navigation, and it actually traps: an assertion on a
    // flag nothing can ever set is an assertion that always passes.
    await page.addInitScript(() => {
      const marker = window as unknown as { __audio?: string[] };
      marker.__audio = [];
      const trap = (name: string, original: unknown) =>
        new Proxy(original as object, {
          construct(target, args) {
            marker.__audio!.push(name);
            return Reflect.construct(target as never, args);
          },
        });
      if (window.Audio) window.Audio = trap("Audio", window.Audio) as typeof Audio;
      if (window.AudioContext) {
        window.AudioContext = trap("AudioContext", window.AudioContext) as typeof AudioContext;
      }
      const speak = window.speechSynthesis?.speak;
      if (speak) {
        window.speechSynthesis.speak = function (...args) {
          marker.__audio!.push("speechSynthesis");
          return speak.apply(this, args);
        };
      }
    });

    await stubBackend(page);
    await page.goto("/");
    await ask(page);
    await page.waitForSelector("[id$='-result']");

    // No media elements, and no script constructed an audio source. A patient
    // reading a medical result must not be startled by one, and an autoplaying
    // sound is a WCAG 1.4.2 failure the moment it lasts over three seconds.
    expect(await page.locator("audio, video").count()).toBe(0);
    expect(
      await page.evaluate(() => (window as unknown as { __audio: string[] }).__audio),
    ).toEqual([]);
  });

  test("reduced motion is honoured on the answer surface", async ({ browser }) => {
    const context = await browser.newContext({ reducedMotion: "reduce" });
    await signIn(context);
    const page = await context.newPage();
    await stubBackend(page);
    await page.goto("/");
    await ask(page);
    await page.waitForSelector("[id$='-result']");

    // Nothing is mid-animation: every animation is either absent or finished,
    // so a reader who asked for stillness gets a stable page.
    const running = await page.evaluate(() =>
      document
        .getAnimations()
        .filter((animation) => animation.playState === "running")
        .map((animation) => (animation.effect as KeyframeEffect)?.target?.tagName ?? "unknown"),
    );
    expect(running).toEqual([]);

    // And the content is all there rather than stuck at an animation's start.
    await expect(page.getByRole("heading", { name: "What the research says" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Sources" })).toBeVisible();
    await context.close();
  });
});
