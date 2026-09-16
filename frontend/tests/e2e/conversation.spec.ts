import { readFileSync } from "node:fs";
import { join } from "node:path";

import AxeBuilder from "@axe-core/playwright";
import type { BrowserContext, Page } from "@playwright/test";
import { expect, test } from "@playwright/test";
import { SignJWT } from "jose";

/**
 * The whole patient experience, in a real browser, against a real build.
 *
 * The backend is not running. Every call the page makes to `/api/proxy/**` is
 * intercepted and answered with a **golden fixture** — the same JSON the Python
 * suite generates from `tests/golden_states.py`. So these tests exercise the
 * real composer, the real SSE parser, the real focus management and the real
 * CSP, against payloads that are by construction the ones the server sends.
 *
 * The suite runs under both Playwright projects: `chromium` (a desktop
 * viewport) and `mobile` (a Pixel 7). Several behaviours differ between them by
 * design — the brief is a side panel on one and a modal dialog on the other —
 * and those tests assert the difference rather than skipping one side.
 */

const JWT_SECRET = "e2e-jwt-secret-0123456789abcdef01";
const FIXTURES = join(process.cwd(), "tests", "fixtures");

function fixture(name: string): unknown {
  return JSON.parse(readFileSync(join(FIXTURES, `${name}.json`), "utf8"));
}

async function signIn(context: BrowserContext) {
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

function sse(...frames: [string, unknown][]): string {
  return frames.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join("");
}

interface StubOptions {
  /** The golden state the turn resolves to. */
  state?: string;
  /** Replaces the whole SSE body, for stage and heartbeat cases. */
  body?: string;
  /** Status for the turn request, to exercise transport failures. */
  status?: number;
  turnCount?: number;
}

/** Answer every proxy call the page makes. */
async function stubBackend(page: Page, options: StubOptions = {}) {
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
    return route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: options.body ?? sse(["envelope", fixture(`envelope.${state}`)]),
    });
  });

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

async function ask(page: Page, question = "what does my HbA1c mean?") {
  await page.getByLabel("Your question or symptoms").fill(question);
  await page.getByRole("button", { name: "Ask", exact: true }).click();
}

test.beforeEach(async ({ context }) => {
  await signIn(context);
});

test.describe("asking a question", () => {
  test("shows the answer, its claims and its numbered sources", async ({ page }) => {
    await stubBackend(page);
    await page.goto("/");
    await ask(page);

    await expect(page.getByRole("heading", { name: "What the research says" })).toBeVisible();
    await expect(
      page.getByText("Your HbA1c reflects your average blood sugar over two to three months."),
    ).toBeVisible();
    await expect(page.getByRole("heading", { name: "Sources" })).toBeVisible();
    await expect(page.getByRole("link", { name: /HbA1c Targets in Adults/ })).toBeVisible();
  });

  test("moves focus to the result heading, without re-announcing the page", async ({ page }) => {
    await stubBackend(page);
    await page.goto("/");
    await ask(page);

    const heading = page.getByRole("heading", { name: "What the research says" });
    await expect(heading).toBeFocused();
    // A heading, not a container and not the body: a screen reader announces
    // "What the research says, heading level 2" and nothing else.
    expect(await heading.evaluate((node) => node.tagName)).toBe("H2");
  });

  test("a citation marker jumps to the source it names", async ({ page }) => {
    await stubBackend(page);
    await page.goto("/");
    await ask(page);

    await page.getByRole("link", { name: "Source 1 for this statement" }).click();
    await expect(page.locator("[id$='-source-1']")).toBeInViewport();
  });

  test("external source links carry noopener, noreferrer and nofollow", async ({ page }) => {
    await stubBackend(page);
    await page.goto("/");
    await ask(page);

    const link = page.getByRole("link", { name: /HbA1c Targets in Adults/ });
    await expect(link).toHaveAttribute("rel", "noopener noreferrer nofollow");
    await expect(link).toHaveAttribute("target", "_blank");
  });

  test("the matched passages open on demand", async ({ page }) => {
    await stubBackend(page);
    await page.goto("/");
    await ask(page);

    const quote = page.getByText("HbA1c reflects average plasma glucose over 2-3 months", {
      exact: true,
    });
    await expect(quote).toBeHidden();
    await page.getByText(/Show the 2 passages/).click();
    await expect(quote).toBeVisible();
  });

  test("the identifier warning stays on screen after an answer arrives", async ({ page }) => {
    await stubBackend(page);
    await page.goto("/");
    await ask(page);
    await expect(page.getByRole("heading", { name: "What the research says" })).toBeVisible();

    await expect(page.getByText("Do not enter anything that identifies you")).toBeVisible();
  });

  test("the identifier warning sits above 'What it will not do', not above the box", async ({
    page,
  }) => {
    // It reads as a standing disclaimer rather than a gate in front of the
    // question. Asserted in document order rather than by pixel position, so a
    // spacing change cannot fail it and a move back to the top cannot pass it.
    await stubBackend(page);
    await page.goto("/");

    const order = await page.evaluate(() => {
      const notice = [...document.querySelectorAll("h2")].find((h) =>
        /identifies you/i.test(h.textContent ?? ""),
      );
      const closing = [...document.querySelectorAll("h2")].find((h) =>
        /what it will not do/i.test(h.textContent ?? ""),
      );
      const field = document.querySelector("#question");
      if (!notice || !closing || !field) return null;
      const follows = (a: Element, b: Element) =>
        Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
      return {
        afterTheField: follows(field, notice),
        beforeTheClosingSection: follows(notice, closing),
      };
    });

    expect(order, "the notice, the field or the closing section is missing").not.toBeNull();
    expect(order!.afterTheField).toBe(true);
    expect(order!.beforeTheClosingSection).toBe(true);
  });
});

test.describe("the stage timeline", () => {
  test("reports progress, then replaces it with the whole result", async ({ page }) => {
    // Stages first, then the envelope, in one body — the client renders them in
    // order as it parses.
    await stubBackend(page, {
      body: sse(
        ["stage", { stage: "checking_question", message: "Checking your question..." }],
        ["stage", { stage: "searching", message: "Searching relevant research..." }],
        ["envelope", fixture("envelope.answer")],
      ),
    });
    await page.goto("/");
    await ask(page);

    // Nothing medical appears before the envelope: the timeline is replaced
    // wholesale rather than filled in progressively.
    await expect(page.getByRole("heading", { name: "What the research says" })).toBeVisible();
    await expect(page.getByText("Searching relevant research...")).toBeHidden();
  });

  test("the progress region is polite, never an alert", async ({ page }) => {
    await stubBackend(page, {
      body: sse(
        ["stage", { stage: "searching", message: "Searching relevant research..." }],
        ["envelope", fixture("envelope.answer")],
      ),
    });
    await page.goto("/");
    // Only the emergency card may interrupt; progress must not compete with it.
    await ask(page);
    await expect(page.getByRole("heading", { name: "What the research says" })).toBeVisible();
    expect(await page.locator("main").locator('[role="alert"]').count()).toBe(0);
  });
});

test.describe("the emergency state", () => {
  test("interrupts with role=alert and cites nothing", async ({ page }) => {
    await stubBackend(page, { state: "emergency" });
    await page.goto("/");
    await ask(page, "crushing chest pain spreading to my arm");

    // Scoped to `main`: Next injects its own empty `role="alert"` route
    // announcer into the body, which is not the application's and never
    // carries content. The emergency card is the only alert the app renders.
    const alert = page.locator("main").getByRole("alert");
    await expect(alert).toHaveCount(1);
    await expect(alert).toBeVisible();
    await expect(alert.getByRole("heading", { name: /Stop and get medical help now/ })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Sources" })).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: /Build an appointment brief/ }),
    ).toHaveCount(0);
  });

  test("a latched follow-up escalates identically", async ({ page }) => {
    await stubBackend(page, { state: "emergency_latched" });
    await page.goto("/");
    await ask(page, "is that serious?");
    await expect(page.locator("main").getByRole("alert")).toBeVisible();
  });
});

test.describe("states that are not answers", () => {
  test("insufficient evidence reads as a result, not a fault", async ({ page }) => {
    await stubBackend(page, { state: "failure_evidence_unavailable" });
    await page.goto("/");
    await ask(page);

    await expect(
      page.getByRole("heading", { name: "Not enough verified information" }),
    ).toBeVisible();
    await expect(page.getByRole("heading", { name: /What you can do/ })).toBeVisible();
    await expect(page.getByText(/Reference code/)).toHaveCount(0);
    // The operator-facing reason code is never shown to a patient.
    await expect(page.getByText("no_eligible_evidence")).toHaveCount(0);
  });

  test("a failure shows fixed copy and a code, and offers a retry", async ({ page }) => {
    await stubBackend(page, { state: "failure_generation_unavailable" });
    await page.goto("/");
    await ask(page);

    await expect(page.getByRole("heading", { name: "Something went wrong" })).toBeVisible();
    await expect(page.getByText("generation_unavailable")).toBeVisible();
    await expect(page.getByRole("button", { name: "Try again" })).toBeVisible();
  });

  test("a failure leaks no provider text", async ({ page }) => {
    await stubBackend(page, { state: "failure_generation_unavailable" });
    await page.goto("/");
    await ask(page);
    await expect(page.getByRole("heading", { name: "Something went wrong" })).toBeVisible();

    const body = (await page.locator("body").textContent()) ?? "";
    expect(body).not.toMatch(/sk-[A-Za-z0-9]/);
    expect(body).not.toContain("api.openai.com");
    expect(body).not.toMatch(/Traceback/);
  });

  test("an abstention keeps the questions and marks the evidence as insufficient", async ({
    page,
  }) => {
    await stubBackend(page, { state: "abstain" });
    await page.goto("/");
    await ask(page);

    await expect(
      page.getByRole("heading", { name: "Not enough verified information" }),
    ).toBeVisible();
    await expect(page.getByRole("heading", { name: /Worth asking/ })).toBeVisible();
  });

  test("a partially supported claim is labelled rather than corrected", async ({ page }) => {
    await stubBackend(page, { state: "partially_supported" });
    await page.goto("/");
    await ask(page);
    // Both by design: a note at the top of the answer, and a badge on the
    // claim. The badge is the exact-match one.
    await expect(page.getByText("Partly supported", { exact: true })).toBeVisible();
    await expect(page.getByText(/Some statements below are only partly supported/)).toBeVisible();
  });

  test("a rewritten follow-up says what it searched for", async ({ page }) => {
    await stubBackend(page, { state: "follow_up_rewritten" });
    await page.goto("/");
    await ask(page, "what about the side effects?");
    await expect(page.getByText(/Read as a follow-up/)).toBeVisible();
    await expect(
      page.getByText("what are the side effects of metformin for type 2 diabetes?"),
    ).toBeVisible();
  });
});

test.describe("error recovery", () => {
  test("keeps the draft and retries it", async ({ page }) => {
    await stubBackend(page, { status: 503 });
    await page.goto("/");

    const question = "a carefully worded description of a symptom";
    await ask(page, question);

    await expect(page.getByText(/service is not available/)).toBeVisible();
    // The whole point of preserving it.
    await expect(page.getByLabel("Your question or symptoms")).toHaveValue(question);

    // Now let it succeed, and retry.
    await page.unroute("**/api/proxy/v1/turns/stream");
    await page.route("**/api/proxy/v1/turns/stream", async (route) =>
      route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: sse(["envelope", fixture("envelope.answer")]),
      }),
    );
    await page.getByRole("button", { name: "Try again" }).click();
    await expect(page.getByRole("heading", { name: "What the research says" })).toBeVisible();
  });

  test("an expired session offers sign-in rather than a doomed retry", async ({ page }) => {
    await stubBackend(page, { status: 401 });
    await page.goto("/");
    await ask(page);

    await expect(page.getByRole("link", { name: "Sign in again" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Try again" })).toHaveCount(0);
  });
});

test.describe("session lifecycle", () => {
  test("a refresh reports the turns the server still holds", async ({ page }) => {
    await stubBackend(page, { turnCount: 2 });
    await page.goto("/");

    await expect(page.getByText(/already has 2 questions/)).toBeVisible();
    await expect(page.getByText(/never stores what was asked or answered/)).toBeVisible();
  });

  test("clearing the conversation empties the page and deletes the session", async ({ page }) => {
    await stubBackend(page);
    await page.goto("/");
    await ask(page);
    await expect(page.getByRole("heading", { name: "What the research says" })).toBeVisible();

    const deleted = page.waitForRequest(
      (request) =>
        request.url().includes("/api/proxy/v1/session") && request.method() === "DELETE",
    );
    await page.getByRole("button", { name: /Clear this conversation/ }).click();
    await deleted;
    await expect(page.getByRole("heading", { name: "What the research says" })).toHaveCount(0);
  });

  test("signing out returns to the passcode screen and drops the cookie", async ({
    page,
    context,
    isMobile,
  }) => {
    await stubBackend(page);
    await page.goto("/transparency");
    // On a narrow viewport the navigation is collapsed, so sign-out is behind
    // the menu button — which is itself part of what this asserts works.
    if (isMobile) await page.getByRole("button", { name: "Menu" }).click();
    await page.getByRole("button", { name: "Sign Out" }).first().click();

    await expect(page).toHaveURL(/\/access$/);
    const cookie = (await context.cookies()).find((item) => item.name === "synapse_access");
    expect(cookie?.value ?? "").toBe("");
  });
});

test.describe("keyboard operation", () => {
  test("the whole turn can be driven from the keyboard", async ({ page }) => {
    await stubBackend(page);
    await page.goto("/");

    await page.getByLabel("Your question or symptoms").focus();
    await page.keyboard.type("what does my HbA1c mean?");
    // Enter sends; the hint under the field says so.
    await page.keyboard.press("Enter");

    await expect(page.getByRole("heading", { name: "What the research says" })).toBeVisible();
  });

  test("Shift+Enter makes a new line instead of sending", async ({ page }) => {
    await stubBackend(page);
    await page.goto("/");

    const field = page.getByLabel("Your question or symptoms");
    await field.focus();
    await page.keyboard.type("first line");
    await page.keyboard.press("Shift+Enter");
    await page.keyboard.type("second line");

    await expect(field).toHaveValue("first line\nsecond line");
    await expect(page.getByRole("heading", { name: "What the research says" })).toHaveCount(0);
  });

  test("every example chip is reachable and operable by keyboard", async ({ page }) => {
    await stubBackend(page);
    await page.goto("/");

    const chip = page.getByRole("button", { name: "What does my HbA1c number actually mean?" });
    await chip.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByLabel("Your question or symptoms")).toHaveValue(
      "What does my HbA1c number actually mean?",
    );
  });
});

test.describe("the composer", () => {
  test("is labelled, and caps the question at the server's limit", async ({ page }) => {
    await stubBackend(page);
    await page.goto("/");

    const field = page.getByLabel("Your question or symptoms");
    await expect(field).toHaveAttribute("maxlength", "2000");

    await field.fill("x".repeat(2500));
    expect((await field.inputValue()).length).toBe(2000);
  });

  test("warns as the limit approaches", async ({ page }) => {
    await stubBackend(page);
    await page.goto("/");
    await page.getByLabel("Your question or symptoms").fill("x".repeat(1950));
    await expect(page.getByText(/50 characters left of 2000/)).toBeVisible();
  });

  test("accepts unrestricted free text, including symptom descriptions", async ({ page }) => {
    // No vocabulary filter: a patient must be able to use their own words.
    await stubBackend(page);
    await page.goto("/");
    const messy = "my chest feels tight & i'm 40% more tired <since tuesday>";
    await page.getByLabel("Your question or symptoms").fill(messy);
    await expect(page.getByLabel("Your question or symptoms")).toHaveValue(messy);
    await expect(page.getByRole("button", { name: "Ask", exact: true })).toBeEnabled();
  });

  test("cannot be submitted twice", async ({ page }) => {
    let requests = 0;
    await stubBackend(page);
    await page.route("**/api/proxy/v1/turns/stream", async (route) => {
      requests += 1;
      await new Promise((resolve) => setTimeout(resolve, 700));
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: sse(["envelope", fixture("envelope.answer")]),
      });
    });
    await page.goto("/");
    await ask(page);

    const button = page.getByRole("button", { name: "Working" });
    await expect(button).toBeDisabled();
    await expect(page.getByLabel("Your question or symptoms")).toBeDisabled();

    await expect(page.getByRole("heading", { name: "What the research says" })).toBeVisible();
    expect(requests).toBe(1);
  });
});

test.describe("the appointment brief", () => {
  test("is a modal dialog on mobile and a side panel on desktop", async ({ page, isMobile }) => {
    await stubBackend(page);
    await page.goto("/");
    await ask(page);
    await page.getByRole("button", { name: /Build an appointment brief/ }).click();

    if (isMobile) {
      // Full-screen: it covers the page, so it must be modal and dismissible.
      const dialog = page.getByRole("dialog");
      await expect(dialog).toBeVisible();
      await expect(dialog).toHaveAttribute("aria-modal", "true");
      await page.keyboard.press("Escape");
      await expect(dialog).toHaveCount(0);
    } else {
      // Beside the answer: the rest of the page stays operable, so claiming to
      // be modal would hide a document the reader can still use.
      await expect(page.getByRole("complementary")).toBeVisible();
      await expect(page.getByRole("dialog")).toHaveCount(0);
    }
  });

  test("takes focus when it opens and returns it when it closes", async ({ page, isMobile }) => {
    await stubBackend(page);
    await page.goto("/");
    await ask(page);

    const trigger = page.getByRole("button", { name: /Build an appointment brief/ });
    await trigger.click();

    const panel = isMobile ? page.getByRole("dialog") : page.getByRole("complementary");
    await expect(panel).toBeFocused();

    // `exact` because the trigger is now "Hide the appointment brief", and
    // Playwright matches accessible names by substring unless told otherwise.
    await page.getByRole("button", { name: isMobile ? "Close" : "Hide", exact: true }).click();
    // Back to what opened it, rather than the top of the document. This works
    // only because the trigger stays mounted as a disclosure button.
    await expect(trigger).toBeFocused();
    await expect(trigger).toHaveAttribute("aria-expanded", "false");
  });

  test("turns an add-on on through one named operation", async ({ page }) => {
    // Replaces a test that filled the topic field and blurred into the notes
    // box. Neither field is on this panel any more: a patient whose question
    // has just been answered was being asked to type it in again.
    await stubBackend(page);
    await page.goto("/");
    await ask(page);
    await page.getByRole("button", { name: /Build an appointment brief/ }).click();

    const request = page.waitForRequest(
      (candidate) => candidate.url().includes("/brief/sections") && candidate.method() === "PUT",
    );
    await page.getByRole("checkbox", { name: /Your conversation/ }).check();
    const sent = await request;
    expect(sent.postDataJSON().sections).toContain("transcript");
  });

  test("offers no topic or notes field", async ({ page }) => {
    await stubBackend(page);
    await page.goto("/");
    await ask(page);
    await page.getByRole("button", { name: /Build an appointment brief/ }).click();

    await expect(page.getByLabel("What you want to talk about")).toHaveCount(0);
    await expect(page.getByLabel("Your notes")).toHaveCount(0);
    await expect(
      page.getByRole("heading", { name: /Would you like to add more questions/ }),
    ).toBeVisible();
  });

  test("adds and reorders questions from the keyboard", async ({ page }) => {
    await stubBackend(page);
    await page.goto("/");
    await ask(page);
    await page.getByRole("button", { name: /Build an appointment brief/ }).click();

    const added = page.waitForRequest(
      (candidate) => candidate.url().endsWith("/questions") && candidate.method() === "POST",
    );
    await page.getByLabel("Add a question of your own").fill("Is this normal?");
    await page.getByRole("button", { name: "Add", exact: true }).click();
    expect((await added).postDataJSON()).toEqual({ text: "Is this normal?" });

    // Reordering is buttons, not dragging, so it works here at all.
    const reordered = page.waitForRequest(
      (candidate) => candidate.url().includes("/questions/order") && candidate.method() === "PUT",
    );
    await page.getByRole("button", { name: /^Move ".*" down$/ }).first().click();
    expect(Array.isArray((await reordered).postDataJSON().order)).toBe(true);
  });

  test("offers all three downloads as real links", async ({ page }) => {
    await stubBackend(page);
    await page.goto("/");
    await ask(page);
    await page.getByRole("button", { name: /Build an appointment brief/ }).click();

    for (const [name, format] of [
      ["Download HTML", "html"],
      ["Download plain text", "text"],
      ["Download JSON", "json"],
    ] as const) {
      await expect(page.getByRole("link", { name })).toHaveAttribute(
        "href",
        `/api/proxy/v1/turns/0/brief/export/${format}`,
      );
    }
  });

  test("an export downloads with the server's filename", async ({ page }) => {
    await stubBackend(page);
    await page.goto("/");
    await ask(page);
    await page.getByRole("button", { name: /Build an appointment brief/ }).click();

    const download = page.waitForEvent("download");
    await page.getByRole("link", { name: "Download plain text" }).click();
    const saved = await download;
    // The server names the file, from the brief's own document id.
    expect(saved.suggestedFilename()).toBe("appointment-brief-e2e.txt");
  });

  test("is absent, not disabled, where no brief exists", async ({ page }) => {
    for (const state of ["emergency", "failure_evidence_unavailable", "failure_internal_error"]) {
      await stubBackend(page, { state });
      await page.goto("/");
      await ask(page);
      await expect(
        page.getByRole("button", { name: /Build an appointment brief/ }),
      ).toHaveCount(0);
      await page.unrouteAll();
    }
  });
});

test.describe("transparency", () => {
  test("states every disclosure even with no backend", async ({ page }) => {
    // The backend genuinely is not running: the page must still disclose.
    await page.goto("/transparency");

    await expect(page.getByRole("heading", { level: 1, name: "About Synapse" })).toBeVisible();
    // Scoped to the article: the site footer carries its own standing
    // disclaimer, which says the same thing and is not what this asserts.
    const article = page.locator("main article");
    await expect(article.getByText(/is not a medical device/)).toBeVisible();
    await expect(article.getByText(/No clinician has reviewed/)).toBeVisible();
    await expect(article.getByText(/sources currently served are UNREVIEWED/)).toBeVisible();
  });

  test("covers intended use, exclusions, safety and what it does not protect", async ({
    page,
  }) => {
    await page.goto("/transparency");
    for (const heading of [
      "What it is for",
      "What it is not for",
      "Where the system stops itself",
      "What this does not protect you from",
    ]) {
      await expect(page.getByRole("heading", { name: heading })).toBeVisible();
    }
  });

  test("no longer publishes the data-flow walkthrough or the build metadata", async ({ page }) => {
    // Removed deliberately. The disclosures that carry weight -- no HIPAA
    // agreement, text leaving for a model provider, a shared passcode -- are
    // kept under "What this does not protect you from", and the identifier
    // warning on the question screen still says the question is sent to a
    // model provider.
    await page.goto("/transparency");
    for (const heading of ["Where your words go", "How it is put together"]) {
      await expect(page.getByRole("heading", { name: heading })).toHaveCount(0);
    }
    const body = (await page.locator("article").textContent()) ?? "";
    for (const label of ["Answer prompt", "Model version pinned", "Questions written to logs"]) {
      expect(body).not.toContain(label);
    }
  });

  test("keeps operator metrics off the patient's answer screen", async ({ page }) => {
    await stubBackend(page);
    await page.goto("/");
    await ask(page);
    await expect(page.getByRole("heading", { name: "What the research says" })).toBeVisible();

    const body = (await page.locator("body").textContent()) ?? "";
    // Readiness, index identity and evaluation counts belong on /transparency,
    // where they inform an assessment — not beside a patient's own result,
    // where they read as a quality score for it.
    expect(body).not.toMatch(/readiness/i);
    expect(body).not.toMatch(/index_id/i);
    expect(body).not.toMatch(/dataset_version/i);
  });
});

test.describe("reduced motion", () => {
  test("the progress indicator is stopped, not sped up", async ({ browser }) => {
    const context = await browser.newContext({ reducedMotion: "reduce" });
    await signIn(context);
    const page = await context.newPage();
    await stubBackend(page, {
      body: sse(
        ["stage", { stage: "searching", message: "Searching relevant research..." }],
        ["envelope", fixture("envelope.answer")],
      ),
    });
    await page.goto("/");
    await ask(page);
    await expect(page.getByRole("heading", { name: "What the research says" })).toBeVisible();

    // The blanket reduced-motion rule sets every animation to 0.01ms, which for
    // a looping one is a flicker. It has to be switched off by name instead.
    const stopped = await page.evaluate(() => {
      const probe = document.createElement("span");
      probe.className = "stage-pulse";
      document.body.append(probe);
      const name = getComputedStyle(probe).animationName;
      probe.remove();
      return name;
    });
    expect(stopped).toBe("none");
    await context.close();
  });

  test("an answer still renders completely", async ({ browser }) => {
    const context = await browser.newContext({ reducedMotion: "reduce" });
    await signIn(context);
    const page = await context.newPage();
    await stubBackend(page);
    await page.goto("/");
    await ask(page);

    await expect(page.getByRole("heading", { name: "What the research says" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Sources" })).toBeVisible();
    await context.close();
  });
});

test.describe("the conversation continues", () => {
  /** True when the question field comes after the newest result in document order. */
  const fieldFollowsNewestAnswer = (page: Page) =>
    page.evaluate(() => {
      const field = document.querySelector("#question");
      const results = document.querySelectorAll("[id$='-result']");
      const newest = results[results.length - 1];
      if (!field || !newest) return null;
      return Boolean(
        newest.compareDocumentPosition(field) & Node.DOCUMENT_POSITION_FOLLOWING,
      );
    });

  test("moves the question box below the answer, and asks again from there", async ({ page }) => {
    await stubBackend(page);
    await page.goto("/");

    // Before anything is asked the box is the page, and there is no answer yet.
    await expect(page.getByLabel("Your question or symptoms")).toBeVisible();
    expect(await fieldFollowsNewestAnswer(page)).toBeNull();

    await ask(page);
    await page.waitForSelector("[id$='-result']");

    // Now it sits after the answer, presented as a continuation.
    expect(await fieldFollowsNewestAnswer(page)).toBe(true);
    await expect(page.getByRole("heading", { name: "Ask a follow-up" })).toBeVisible();

    await ask(page, "what about the side effects?");
    await expect(page.locator("[id$='-result']")).toHaveCount(2);
    // Both questions are still on the page, in the order they were asked.
    // `exact` because the follow-up help copy quotes this very phrase as its
    // example, and a substring match would find that instead.
    await expect(page.getByText("what does my HbA1c mean?", { exact: true })).toBeVisible();
    await expect(page.getByText("what about the side effects?", { exact: true })).toBeVisible();
    // And the box has followed the conversation down.
    expect(await fieldFollowsNewestAnswer(page)).toBe(true);
  });

  test("still moves focus to the newest result heading on a follow-up", async ({ page }) => {
    await stubBackend(page);
    await page.goto("/");
    await ask(page);
    await page.waitForSelector("[id$='-result']");
    await ask(page, "what about the side effects?");
    await expect(page.locator("[id$='-result']")).toHaveCount(2);

    // The invariant the first turn already had, preserved across the move: a
    // screen reader announces the new heading, not the page and not the box.
    const newest = page.locator("[id$='-result']").last();
    await expect(newest).toBeFocused();
    expect(await newest.evaluate((node) => node.tagName)).toBe("H2");
  });

  test("has exactly one question field once the conversation is under way", async ({ page }) => {
    await stubBackend(page);
    await page.goto("/");
    await ask(page);
    await page.waitForSelector("[id$='-result']");

    // Two would mean a duplicated id and two identically labelled boxes.
    await expect(page.locator("#question")).toHaveCount(1);
    await expect(page.getByLabel("Your question or symptoms")).toHaveCount(1);
  });

  test("the follow-up box is enabled and empty after an answer", async ({ page }) => {
    await stubBackend(page);
    await page.goto("/");
    await ask(page);
    await page.waitForSelector("[id$='-result']");

    const field = page.getByLabel("Your question or symptoms");
    await expect(field).toBeEnabled();
    await expect(field).toHaveValue("");
    await field.fill("a follow-up");
    await expect(page.getByRole("button", { name: "Ask", exact: true })).toBeEnabled();
  });
});

test.describe("accessibility", () => {
  const scan = (page: Page) =>
    new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
      .analyze();

  test("a continued conversation has no detectable violations", async ({ page }) => {
    await stubBackend(page);
    await page.goto("/");
    await ask(page);
    await page.waitForSelector("[id$='-result']");
    await ask(page, "what about the side effects?");
    await expect(page.locator("[id$='-result']")).toHaveCount(2);

    const results = await scan(page);
    expect(results.violations).toEqual([]);
  });

  for (const state of ["answer", "emergency", "abstain", "failure_internal_error"]) {
    test(`the ${state} state has no detectable violations`, async ({ page }) => {
      await stubBackend(page, { state });
      await page.goto("/");
      await ask(page);
      await page.waitForSelector("[id$='-result']");

      const results = await scan(page);
      expect(results.violations).toEqual([]);
    });
  }

  test("the appointment brief has no detectable violations", async ({ page }) => {
    await stubBackend(page);
    await page.goto("/");
    await ask(page);
    await page.getByRole("button", { name: /Build an appointment brief/ }).click();
    await page.getByRole("heading", { name: /Your appointment brief/ }).waitFor();

    const results = await scan(page);
    expect(results.violations).toEqual([]);
  });

  test("headings never skip a level on a rendered answer", async ({ page }) => {
    await stubBackend(page);
    await page.goto("/");
    await ask(page);
    await expect(page.getByRole("heading", { name: "What the research says" })).toBeVisible();

    const levels = await page
      .locator("h1, h2, h3, h4, h5, h6")
      .evaluateAll((nodes) => nodes.map((node) => Number(node.tagName[1])));
    expect(levels[0]).toBe(1);
    for (let index = 1; index < levels.length; index += 1) {
      expect(levels[index]! - levels[index - 1]!).toBeLessThanOrEqual(1);
    }
  });
});
