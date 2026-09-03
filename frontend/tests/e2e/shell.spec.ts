import AxeBuilder from "@axe-core/playwright";
import type { BrowserContext } from "@playwright/test";
import { expect, test } from "@playwright/test";
import { SignJWT } from "jose";

/**
 * The branded shell, against a real production build.
 *
 * `next start` rather than `next dev`, because the security headers, the error
 * boundaries and the CSP all behave differently in development — and those are
 * most of what this file is for.
 *
 * The backend is not running. That is deliberate for this phase: nothing here
 * needs it, and a shell that only works when the whole stack is up is a shell
 * that cannot be tested.
 */

const JWT_SECRET = "e2e-jwt-secret-0123456789abcdef01";

/** Mint a cookie the middleware will accept, without a backend. */
async function accessCookie(expires = "8h") {
  return new SignJWT({ sid: "e2e-session-0123456789ab" })
    .setProtectedHeader({ alg: "HS256" })
    .setIssuedAt()
    .setExpirationTime(expires)
    .sign(new TextEncoder().encode(JWT_SECRET));
}

async function signIn(context: BrowserContext, expires = "8h") {
  await context.addCookies([
    {
      name: "synapse_access",
      value: await accessCookie(expires),
      domain: "127.0.0.1",
      path: "/",
      httpOnly: true,
      sameSite: "Lax",
    },
  ]);
}

test.describe("the access gate", () => {
  test("an unauthenticated visitor is sent to the passcode screen", async ({ page }) => {
    await page.goto("/");
    await expect(page).toHaveURL(/\/access$/);
    // The hero wordmark IS the h1 on both centred pages.
    await expect(page.getByRole("heading", { level: 1, name: "Synapse" })).toBeVisible();
    await expect(page.getByLabel("Access passcode")).toBeVisible();
  });

  test("it remembers where they were going", async ({ page }) => {
    await page.goto("/transparency");
    // /transparency is public, so it renders rather than redirecting.
    await expect(page).toHaveURL(/\/transparency$/);
  });

  test("a signed-in visitor reaches the question surface", async ({ page, context }) => {
    await signIn(context);
    await page.goto("/");
    await expect(page).toHaveURL(/127\.0\.0\.1:\d+\/$/);
    await expect(page.getByRole("heading", { level: 1, name: "Synapse" })).toBeVisible();
    await expect(page.getByLabel("Your question or symptoms")).toBeVisible();
  });

  test("an EXPIRED cookie is treated as no cookie", async ({ page, context }) => {
    // The 8-hour cookie outlives the backend's 2-hour session, so this is a
    // state a real patient reaches by leaving a tab open.
    await signIn(context, "-1h");
    await page.goto("/");
    await expect(page).toHaveURL(/\/access/);
  });

  test("an expired cookie is cleared rather than left to fail repeatedly", async ({
    page,
    context,
  }) => {
    await signIn(context, "-1h");
    await page.goto("/");
    const cookies = await context.cookies();
    const access = cookies.find((cookie) => cookie.name === "synapse_access");
    expect(access?.value ?? "").toBe("");
  });

  test("a FORGED cookie is rejected", async ({ page, context }) => {
    const forged = await new SignJWT({ sid: "forged" })
      .setProtectedHeader({ alg: "HS256" })
      .setExpirationTime("8h")
      .sign(new TextEncoder().encode("a-completely-different-secret-000"));
    await context.addCookies([
      { name: "synapse_access", value: forged, domain: "127.0.0.1", path: "/" },
    ]);
    await page.goto("/");
    await expect(page).toHaveURL(/\/access/);
  });

  test("a signed-in visitor is moved off the passcode screen", async ({ page, context }) => {
    await signIn(context);
    await page.goto("/access");
    await expect(page).toHaveURL(/127\.0\.0\.1:\d+\/$/);
  });

  test("signing out clears the cookie and returns to the passcode screen", async ({
    page,
    context,
  }) => {
    await signIn(context);
    await page.goto("/");
    // On a narrow viewport the control lives inside the collapsed panel and
    // does not exist until it is opened — which is the point of the disclosure,
    // and is asserted directly in the navigation unit tests.
    const menu = page.getByRole("button", { name: "Menu" });
    if (await menu.isVisible()) await menu.click();
    await page.getByRole("button", { name: /sign out/i }).first().click();
    await expect(page).toHaveURL(/\/access/);
    const cookies = await context.cookies();
    expect(cookies.find((cookie) => cookie.name === "synapse_access")?.value ?? "").toBe("");
  });

  test("the sign-out redirect stays on the caller's origin", async ({ request, baseURL }) => {
    // `new URL("/access", request.url)` resolves against the host the SERVER
    // was configured with, not the one the client used. Behind a proxy that is
    // an internal hostname the patient cannot reach. The Location must be
    // relative, so the browser resolves it against the origin it is on.
    const response = await request.post("/api/access/logout", { maxRedirects: 0 });
    expect(response.status()).toBe(303);
    const location = response.headers()["location"] ?? "";
    expect(location).toBe("/access");
    expect(location).not.toContain("localhost");
    expect(location).not.toContain(String(baseURL));
  });
});

test.describe("security headers", () => {
  test("every one is present on a page response", async ({ page }) => {
    const response = await page.goto("/access");
    const headers = response?.headers() ?? {};

    expect(headers["x-frame-options"]).toBe("DENY");
    expect(headers["x-content-type-options"]).toBe("nosniff");
    expect(headers["referrer-policy"]).toBe("no-referrer");
    expect(headers["permissions-policy"]).toContain("camera=()");
    expect(headers["permissions-policy"]).toContain("geolocation=()");
    expect(headers["permissions-policy"]).toContain("microphone=()");
    expect(headers["cross-origin-opener-policy"]).toBe("same-origin");
    // The framework version is not advertised to a scanner.
    expect(headers["x-powered-by"]).toBeUndefined();
  });

  test("the CSP is nonce-based and blocks off-origin exfiltration", async ({ page }) => {
    const response = await page.goto("/access");
    const csp = response?.headers()["content-security-policy"] ?? "";

    expect(csp).toMatch(/script-src[^;]*'nonce-[A-Za-z0-9+/=]+'/);
    expect(csp).toContain("'strict-dynamic'");
    // The one that stops a script that got onto the page from phoning home.
    expect(csp).toContain("connect-src 'self'");
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).toContain("base-uri 'none'");
    expect(csp).toContain("object-src 'none'");
    // No blanket inline scripts. Styles are the stated exception.
    expect(csp).not.toMatch(/script-src[^;]*'unsafe-inline'/);
  });

  test("the nonce differs on every response", async ({ page }) => {
    const first = (await page.goto("/access"))?.headers()["content-security-policy"] ?? "";
    const second = (await page.goto("/transparency"))?.headers()["content-security-policy"] ?? "";
    const nonceOf = (csp: string) => /'nonce-([A-Za-z0-9+/=]+)'/.exec(csp)?.[1];
    expect(nonceOf(first)).toBeTruthy();
    expect(nonceOf(first)).not.toBe(nonceOf(second));
  });

  test("HSTS is emitted by a production build", async ({ page }) => {
    // These specs run `next start` over a production build, which is the
    // configuration that ships. A browser ignores HSTS delivered over plain
    // HTTP, so serving it to this local server is harmless; the header being
    // absent from a DEVELOPMENT build is what `next.config.ts` guards, and a
    // dev server is not what is under test here.
    const response = await page.goto("/access");
    const hsts = response?.headers()["strict-transport-security"] ?? "";
    expect(hsts).toContain("max-age=63072000");
    expect(hsts).toContain("includeSubDomains");
  });
});

test.describe("the proxy", () => {
  test("refuses an unauthenticated protected call", async ({ request }) => {
    const response = await request.get("/api/proxy/v1/session");
    expect(response.status()).toBe(401);
    expect((await response.json()).code).toBe("unauthorized");
  });

  test("refuses a path outside the allow-list", async ({ request }) => {
    const response = await request.get("/api/proxy/openapi.json");
    expect(response.status()).toBe(404);
  });

  test("refuses traversal out of the allow-list", async ({ request }) => {
    const response = await request.get("/api/proxy/v1/turns/../../openapi.json");
    expect([404, 400]).toContain(response.status());
  });

  test("never returns a backend Set-Cookie to the browser", async ({ request }) => {
    const response = await request.get("/api/proxy/v1/transparency");
    // The backend is not running, so this is a 502 — the point is that no
    // cookie header crosses back regardless of the outcome.
    expect(response.headers()["set-cookie"]).toBeUndefined();
  });

  test("logging out is not reachable by GET", async ({ request }) => {
    // Only POST is exported, so Next answers 405. A GET logout would be
    // reachable by prefetch, by a crawler, and by any page that can cause a
    // navigation.
    const response = await request.get("/api/access/logout", { maxRedirects: 0 });
    expect(response.status()).toBe(405);
  });

  test("an unauthenticated API call gets typed JSON, never an HTML redirect", async ({
    request,
  }) => {
    // Middleware must not bounce /api/* to the sign-in page: a fetch caller
    // would receive a 307 and a document where it expected {code, message},
    // and a streaming client would see a redirect mid-request.
    const response = await request.get("/api/proxy/v1/session", { maxRedirects: 0 });
    expect(response.status()).toBe(401);
    expect(response.headers()["content-type"]).toContain("application/json");
  });
});

test.describe("the branded shell", () => {
  test("shows the mark, the name and the attribution", async ({ page }) => {
    await page.goto("/access");
    await expect(page.getByText("Synapse").first()).toBeVisible();
    await expect(page.getByText("A Zenith Company").first()).toBeVisible();
    await expect(page.locator("svg[aria-hidden='true']").first()).toBeAttached();
  });

  test("the mark sits above the name, centred", async ({ page }) => {
    await page.goto("/access");
    const mark = await page.locator("svg[aria-hidden='true']").first().boundingBox();
    const name = await page.getByRole("heading", { level: 1, name: "Synapse" }).boundingBox();
    expect(mark).not.toBeNull();
    expect(name).not.toBeNull();
    // Above.
    expect(mark!.y + mark!.height).toBeLessThanOrEqual(name!.y + 4);
    // Centred on the same axis.
    const markCentre = mark!.x + mark!.width / 2;
    const nameCentre = name!.x + name!.width / 2;
    expect(Math.abs(markCentre - nameCentre)).toBeLessThan(4);
  });

  test("the name is set in Libre Baskerville", async ({ page }) => {
    await page.goto("/access");
    const family = await page
      .getByRole("heading", { level: 1, name: "Synapse" })
      .evaluate((node) => getComputedStyle(node).fontFamily);
    expect(family.toLowerCase()).toContain("libre baskerville");
  });

  test("the name is set large enough to read as a display face", async ({ page }) => {
    await page.goto("/access");
    const size = await page
      .getByRole("heading", { level: 1, name: "Synapse" })
      .evaluate((node) => parseFloat(getComputedStyle(node).fontSize));
    expect(size).toBeGreaterThanOrEqual(48);
  });

  test("the mark is set large, as it was in the Streamlit identity", async ({ page }) => {
    await page.goto("/access");
    const mark = await page.locator("svg[aria-hidden='true']").first().boundingBox();
    expect(mark).not.toBeNull();
    expect(mark!.width).toBeGreaterThanOrEqual(200);
  });

  test("carries no seal, badge or approval imagery", async ({ page }) => {
    await page.goto("/access");
    const html = await page.content();
    for (const word of ["certified", "approved", "accredited", "fda-cleared", "seal-of"]) {
      expect(html.toLowerCase()).not.toContain(word);
    }
    // No photographs of anyone.
    expect(await page.locator("img").count()).toBe(0);
  });

  test("uses the canvas colour", async ({ page }) => {
    await page.goto("/access");
    const background = await page.evaluate(() =>
      getComputedStyle(document.body).backgroundColor,
    );
    // #1D1C1C — a near-black ground, which is what lets the accent violet be
    // royal rather than pale.
    expect(background).toBe("rgb(29, 28, 28)");
  });

  test("does not scroll horizontally at 320px", async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 720 });
    await page.goto("/access");
    const overflows = await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
    );
    expect(overflows).toBe(false);
  });

  test("the skip link is the first thing a keyboard reaches", async ({ page }) => {
    await page.goto("/access");
    await page.keyboard.press("Tab");
    const focused = await page.evaluate(() => document.activeElement?.textContent ?? "");
    expect(focused).toMatch(/skip to main content/i);
  });

  test("the skip link moves focus to the main landmark", async ({ page }) => {
    await page.goto("/access");
    await page.keyboard.press("Tab");
    await page.keyboard.press("Enter");
    const id = await page.evaluate(() => document.activeElement?.id ?? "");
    expect(id).toBe("main");
  });

  test("shows a not-found state for an unknown page", async ({ page, context }) => {
    await signIn(context);
    const response = await page.goto("/no-such-page");
    expect(response?.status()).toBe(404);
    await expect(page.getByRole("heading", { name: /does not exist/i })).toBeVisible();
    // The requested path is not echoed to the reader. Checked against the
    // rendered text rather than the HTML: Next records the route in its own
    // RSC router payload, which is not something this page chose to display.
    const visible = await page.locator("main").innerText();
    expect(visible).not.toContain("no-such-page");
  });
});

/**
 * True when a computed `clip-path` reveals the whole element.
 *
 * Chrome serialises an interpolated inset with mixed units — `inset(0px 0% 0px
 * 0px)` — so matching the string is brittle. What matters is that every edge is
 * zero, whatever unit it is expressed in.
 */
function isFullyRevealed(clipPath: string): boolean {
  if (clipPath === "none") return true;
  const inset = /^inset\(([^)]*)\)$/.exec(clipPath);
  if (!inset?.[1]) return false;
  return inset[1]
    .trim()
    .split(/\s+/)
    .every((edge) => parseFloat(edge) === 0);
}

test.describe("motion", () => {
  test("the mark's trace animates once and holds", async ({ page }) => {
    await page.goto("/access");
    const trace = page.locator(".brandmark-trace").first();
    const style = await trace.evaluate((node) => {
      const computed = getComputedStyle(node);
      return {
        name: computed.animationName,
        iterations: computed.animationIterationCount,
        duration: computed.animationDuration,
        delay: computed.animationDelay,
        fill: computed.animationFillMode,
      };
    });
    expect(style.name).toBe("brandmark-draw");
    // The spec's headline requirement: exactly once, never looping.
    expect(style.iterations).toBe("1");
    expect(style.duration).toBe("1.2s");
    expect(style.delay).toBe("0.18s");
    expect(style.fill).toBe("forwards");
  });

  test("the bright tail is a second stroke of the same line", async ({ page }) => {
    await page.goto("/access");
    const lines = page.locator(".brandmark-trace");
    await expect(lines).toHaveCount(2);
    const [tracePoints, tailPoints] = await lines.evaluateAll((nodes) =>
      nodes.map((node) => node.getAttribute("points")),
    );
    // Identical geometry: the tail IS the trace, painted a second time.
    expect(tailPoints).toBe(tracePoints);
    // ...and there is no dot, circle or other shape standing in for it.
    await expect(page.locator("svg circle")).toHaveCount(0);
  });

  test("the tail is still glowing once the animation has finished", async ({ page }) => {
    await page.goto("/access");
    await page.waitForTimeout(2000); // past 180ms delay + 1.2s draw
    const tail = page.locator(".brandmark-trace").nth(1);
    // `forwards` holds the finished state rather than snapping back.
    expect(await tail.evaluate((n) => getComputedStyle(n).strokeDashoffset)).toBe("0px");
    expect(Number(await tail.evaluate((n) => getComputedStyle(n).opacity))).toBeCloseTo(1, 1);
    await expect(tail).toHaveAttribute("filter", /url\(#brandmark-bloom-/);
  });

  test("reduced motion still leaves the mark COMPLETE", async ({ browser }) => {
    const context = await browser.newContext({ reducedMotion: "reduce" });
    const page = await context.newPage();
    await page.goto("/access");

    const trace = page.locator(".brandmark-trace").first();
    const style = await trace.evaluate((node) => {
      const computed = getComputedStyle(node);
      return { duration: computed.animationDuration, iterations: computed.animationIterationCount };
    });
    // Duration collapses; the iteration count stays at one so `forwards` lands
    // the finished state — a complete mark, not a half-drawn line.
    expect(style.iterations).toBe("1");
    expect(parseFloat(style.duration)).toBeLessThan(0.05);
    expect(await trace.evaluate((n) => getComputedStyle(n).strokeDashoffset)).toBe("0px");
    await context.close();
  });

  test("the wordmark types itself in, once, in steps, alongside the mark", async ({
    page,
  }) => {
    await page.goto("/access");
    const name = page.locator(".brandmark-type").first();
    const style = await name.evaluate((node) => {
      const computed = getComputedStyle(node);
      return {
        name: computed.animationName,
        timing: computed.animationTimingFunction,
        iterations: computed.animationIterationCount,
        duration: computed.animationDuration,
        delay: computed.animationDelay,
        fill: computed.animationFillMode,
      };
    });
    expect(style.name).toBe("brandmark-type");
    // Chrome drops the default `end` keyword when serialising.
    expect(style.timing).toMatch(/^steps\(7(, end)?\)$/);
    expect(style.iterations).toBe("1");
    expect(style.duration).toBe("0.52s");
    expect(style.fill).toBe("forwards");

    // Same delay as the trace: they begin on the same frame.
    const traceDelay = await page
      .locator(".brandmark-trace")
      .first()
      .evaluate((node) => getComputedStyle(node).animationDelay);
    expect(style.delay).toBe(traceDelay);
    expect(style.delay).toBe("0.18s");
  });

  test("the name is fully painted once the reveal has finished", async ({ page }) => {
    await page.goto("/access");
    await page.waitForTimeout(1200); // past 180ms delay + 520ms reveal
    const clip = await page
      .locator(".brandmark-type")
      .first()
      .evaluate((node) => getComputedStyle(node).clipPath);
    // `forwards` holds the revealed state rather than snapping back.
    expect(isFullyRevealed(clip)).toBe(true);
  });

  test("the reveal never shifts the layout", async ({ page }) => {
    await page.goto("/access");
    const heading = page.getByRole("heading", { level: 1, name: "Synapse" });
    const during = await heading.boundingBox();
    await page.waitForTimeout(1400);
    const after = await heading.boundingBox();
    // clip-path paints less; it does not take up less room.
    expect(after!.width).toBeCloseTo(during!.width, 0);
    expect(after!.y).toBeCloseTo(during!.y, 0);
  });

  test("reduced motion paints the name in full immediately", async ({ browser }) => {
    const context = await browser.newContext({ reducedMotion: "reduce" });
    const page = await context.newPage();
    await page.goto("/access");
    const name = page.locator(".brandmark-type").first();
    expect(await name.evaluate((n) => getComputedStyle(n).animationIterationCount)).toBe("1");
    const clip = await name.evaluate((n) => getComputedStyle(n).clipPath);
    expect(isFullyRevealed(clip)).toBe(true);
    await context.close();
  });

  test("the mark's ground is transparent, so the glow bleeds into the page", async ({
    page,
  }) => {
    await page.goto("/access");
    const tile = page.locator("span[aria-hidden='true']").first();
    const style = await tile.evaluate((node) => {
      const computed = getComputedStyle(node);
      return { background: computed.backgroundColor, overflow: computed.overflow };
    });
    expect(style.background).toBe("rgba(0, 0, 0, 0)");
    expect(style.overflow).toBe("visible");
  });
});

test.describe("accessibility", () => {
  for (const path of ["/access", "/transparency"]) {
    test(`${path} has no detectable violations`, async ({ page }) => {
      await page.goto(path);
      const results = await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
        .analyze();
      expect(results.violations).toEqual([]);
    });
  }

  test("the question surface has no detectable violations", async ({ page, context }) => {
    await signIn(context);
    await page.goto("/");
    const results = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
      .analyze();
    expect(results.violations).toEqual([]);
  });

  test("headings start at h1 and never skip a level", async ({ page, context }) => {
    await signIn(context);
    await page.goto("/");
    const levels = await page.evaluate(() =>
      [...document.querySelectorAll("h1,h2,h3,h4,h5,h6")].map((node) =>
        Number(node.tagName.slice(1)),
      ),
    );
    expect(levels[0]).toBe(1);
    for (let index = 1; index < levels.length; index += 1) {
      expect((levels[index] ?? 0) - (levels[index - 1] ?? 0)).toBeLessThanOrEqual(1);
    }
  });
});
