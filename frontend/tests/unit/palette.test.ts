import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * The palette, measured rather than eyeballed.
 *
 * The values are read out of `globals.css` itself, not copied here — a test
 * against a duplicate of the stylesheet proves the duplicate is fine. This is
 * the same principle as `synapse/a11y/palette.py`, which generates the CSS it
 * measures so the two cannot drift.
 *
 * The first replacement value chosen for a border in the previous identity
 * measured 1.80:1 and looked fine on the author's monitor. That is precisely
 * why this is arithmetic and not judgement.
 */

// Resolved from the working directory: under the jsdom environment
// `import.meta.url` is not a file: URL, so it cannot be handed to fs.
const CSS = readFileSync(resolve(process.cwd(), "src/app/globals.css"), "utf8");

/**
 * The stylesheet with `/* ... *\/` comments stripped.
 *
 * The comments explain at length what this identity deliberately does NOT have,
 * so a search over the raw text matches that explanation and makes the
 * exclusion tests vacuous. They must read declarations, not prose.
 */
const DECLARATIONS = CSS.replace(/\/\*[\s\S]*?\*\//g, "");

/** Pull a `--color-x: #rrggbb;` value out of the stylesheet. */
function token(name: string): string {
  const match = new RegExp(`--color-${name}:\\s*(#[0-9a-fA-F]{6})`).exec(CSS);
  if (!match?.[1]) throw new Error(`token --color-${name} not found in globals.css`);
  return match[1].toLowerCase();
}

/** WCAG 2.x relative luminance. */
function luminance(hex: string): number {
  const channels = [1, 3, 5].map((offset) => {
    const value = parseInt(hex.slice(offset, offset + 2), 16) / 255;
    return value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  }) as [number, number, number];
  return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
}

function contrast(a: string, b: string): number {
  const [light, dark] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number];
  return (light + 0.05) / (dark + 0.05);
}

const AA_TEXT = 4.5;
const AA_NON_TEXT = 3.0;

describe("the palette is exactly as specified", () => {
  it.each([
    ["canvas", "#1d1c1c"],
    ["surface", "#252424"],
    ["ink", "#ffffff"],
    ["ink-secondary", "#c9c6cc"],
    ["display", "#ffffff"],
    ["accent", "#a78bfa"],
    ["accent-soft", "#8b5cf6"],
    ["accent-royal", "#7e3ff2"],
    ["border", "#736e73"],
    // Inverted for the dark ground: light ink on a deep tinted well.
    ["emergency", "#fca5a5"],
    ["emergency-surface", "#3a2222"],
    ["warning", "#fcd34d"],
    ["warning-surface", "#382e1c"],
  ])("--color-%s is %s", (name, expected) => {
    expect(token(name)).toBe(expected);
  });

  it("carries the violet family from the Streamlit identity", () => {
    // The near-black ground is what lets these be royal rather than pale, and
    // both are accents the previous interface used on its own dark ground.
    expect(token("accent")).toBe("#a78bfa"); // its citation and link accent
    expect(token("accent-soft")).toBe("#8b5cf6"); // its primary button border
  });
});

describe("every pairing meets WCAG AA", () => {
  it.each([
    ["ink on canvas", "ink", "canvas", AA_TEXT],
    ["ink on surface", "ink", "surface", AA_TEXT],
    ["secondary on canvas", "ink-secondary", "canvas", AA_TEXT],
    ["secondary on surface", "ink-secondary", "surface", AA_TEXT],
    ["display serif on canvas", "display", "canvas", AA_TEXT],
    ["display serif on surface", "display", "surface", AA_TEXT],
    ["ink on sunken", "ink", "sunken", AA_TEXT],
    ["accent on canvas", "accent", "canvas", AA_TEXT],
    ["accent on surface", "accent", "surface", AA_TEXT],
    ["ink on its wash", "ink", "accent-wash", AA_TEXT],
    ["emergency on its surface", "emergency", "emergency-surface", AA_TEXT],
    ["warning on its surface", "warning", "warning-surface", AA_TEXT],
    // Borders are a control boundary, held to the non-text threshold (1.4.11).
    ["border on surface", "border", "surface", AA_NON_TEXT],
    ["border on canvas", "border", "canvas", AA_NON_TEXT],
    ["accent-soft border on surface", "accent-soft", "surface", AA_NON_TEXT],
    ["accent-soft border on canvas", "accent-soft", "canvas", AA_NON_TEXT],
  ])("%s", (_label, fg, bg, required) => {
    expect(contrast(token(fg), token(bg))).toBeGreaterThanOrEqual(required);
  });

  it("puts white ink on the royal filled control", () => {
    expect(contrast("#ffffff", token("accent-royal"))).toBeGreaterThanOrEqual(AA_TEXT);
  });

  it("keeps the filled violet visible against the ground it sits on", () => {
    // A button whose fill matches the page has no edge. 1.4.11 again.
    expect(contrast(token("accent-royal"), token("canvas"))).toBeGreaterThanOrEqual(AA_NON_TEXT);
  });

  it("keeps accent-soft OUT of text roles", () => {
    // #8B5CF6 clears the 3:1 non-text threshold for a border but is nowhere
    // near 4.5:1 for text. The split is the reason there are two accent tokens
    // rather than one.
    expect(contrast(token("accent-soft"), token("surface"))).toBeGreaterThanOrEqual(AA_NON_TEXT);
    expect(contrast(token("accent-soft"), token("surface"))).toBeLessThan(AA_TEXT);
  });
});

describe("the identity's stated exclusions hold", () => {
  it("declares no ambient streak animation", () => {
    // The previous identity's decorative streaks were removed in the
    // accessibility audit and are not coming back. Checked over declarations:
    // the comments discuss their absence by name.
    expect(DECLARATIONS).not.toMatch(/streak/i);
  });

  it("declares only the three animations this interface is allowed", () => {
    // Two startup animations — the mark drawing itself, the name typing in —
    // plus the progress indicator. Any FOURTH keyframe is a new piece of motion
    // in an interface that has deliberately almost none.
    const keyframes = DECLARATIONS.match(/@keyframes\s+([\w-]+)/g) ?? [];
    expect(keyframes.sort()).toEqual([
      "@keyframes brandmark-draw",
      "@keyframes brandmark-type",
      "@keyframes stage-pulse",
    ]);
  });

  it("loops nothing except the one indicator that reports live state", () => {
    // The rule is "everything that moves, moves exactly once", and the single
    // exception is stated rather than the rule being dropped. A turn has no
    // measurable progress and can run for two minutes, so the indicator that
    // says the system is still working has to outlast any fixed duration.
    //
    // It earns the exception by being bounded in a way the others are not: it
    // exists only while a turn is pending, and unmounts when the turn ends.
    // Nothing on an idle page loops.
    const loops = DECLARATIONS.split("\n").filter(
      (line) => /animation[^;]*infinite/.test(line) && !line.trimStart().startsWith("*"),
    );
    expect(loops).toHaveLength(1);
    expect(loops[0]).toMatch(/stage-pulse/);
  });

  it("stops the looping indicator under reduced motion rather than just speeding it up", () => {
    // The blanket `animation-duration: 0.01ms !important` would leave an
    // infinite animation running at 0.01ms — flickering, which is worse than
    // the original. It is switched off by name instead.
    const reduced = DECLARATIONS.slice(
      DECLARATIONS.indexOf("@media (prefers-reduced-motion: reduce)"),
    );
    expect(reduced).toMatch(/\.stage-pulse\s*\{[^}]*animation:\s*none/);
    expect(reduced).toMatch(/\.stage-pulse\s*\{[^}]*opacity:\s*1/);
  });

  it("stops all motion under prefers-reduced-motion", () => {
    expect(DECLARATIONS).toMatch(/@media\s*\(prefers-reduced-motion:\s*reduce\)/);
    expect(DECLARATIONS).toMatch(/animation-duration:\s*0\.01ms\s*!important/);
  });

  it("leaves the mark COMPLETE under reduced motion, not half-drawn", () => {
    // Duration collapses and iteration count stays at 1, so `forwards` lands
    // both the trace and the bloom in their finished state.
    const reduced = DECLARATIONS.slice(
      DECLARATIONS.indexOf("@media (prefers-reduced-motion: reduce)"),
    );
    expect(reduced).toMatch(/\.brandmark-trace,\s*\n?\s*\.brandmark-type\s*\{/);
    expect(reduced).toMatch(/animation-duration:\s*0\.01ms/);
    expect(reduced).toMatch(/animation-iteration-count:\s*1/);
  });

  it("uses radii between 12 and 16 pixels", () => {
    const radii = [...DECLARATIONS.matchAll(/--radius-[\w-]+:\s*(\d+)px/g)].map((m) =>
      Number(m[1]),
    );
    expect(radii.length).toBeGreaterThan(0);
    for (const radius of radii) {
      expect(radius).toBeGreaterThanOrEqual(12);
      expect(radius).toBeLessThanOrEqual(16);
    }
  });
});
