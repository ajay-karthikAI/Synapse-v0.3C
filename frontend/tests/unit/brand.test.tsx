import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { BrandMark } from "@/components/brand/BrandMark";
import { Wordmark } from "@/components/brand/Wordmark";

/**
 * The mark, asserted where it carries meaning rather than taste.
 *
 * The geometry and the gradient stops are pinned to exact values because the
 * mark is the one thing recognisable across the Streamlit interface and this
 * one. A mark that drifts is a different mark, and "it still looks about right"
 * is not a check.
 *
 * The animation's *computed* iteration count cannot be asserted here — jsdom
 * does not load `globals.css`, so nothing is computed. This file asserts the
 * DECLARATION by reading the stylesheet; `tests/e2e/shell.spec.ts` asserts the
 * computed value in a real browser. Both are needed: one catches a rule that
 * was never written, the other catches one that is overridden.
 */

const CSS = readFileSync(resolve(process.cwd(), "src/app/globals.css"), "utf8");

const EXPECTED_POINTS = "8,100 65,100 78,100 92,36 108,164 122,100 138,100 192,100";

describe("the trace geometry", () => {
  it("is the original identity's, point for point", () => {
    const { container } = render(<BrandMark />);
    expect(container.querySelector("polyline")).toHaveAttribute("points", EXPECTED_POINTS);
  });

  it("is an unfilled stroke with round joins", () => {
    const { container } = render(<BrandMark />);
    const line = container.querySelector("polyline");
    expect(line).toHaveAttribute("fill", "none");
    expect(line).toHaveAttribute("stroke-width", "4");
    expect(line).toHaveAttribute("stroke-linecap", "round");
    expect(line).toHaveAttribute("stroke-linejoin", "round");
  });

  it("normalises its length to 600 so the dash animation is geometry-independent", () => {
    const { container } = render(<BrandMark />);
    expect(container.querySelector("polyline")).toHaveAttribute("pathLength", "600");
  });

  it("draws on a 200-unit square viewBox", () => {
    const { container } = render(<BrandMark />);
    expect(container.querySelector("svg")).toHaveAttribute("viewBox", "0 0 200 200");
  });
});

describe("the gradient", () => {
  function stops() {
    const { container } = render(<BrandMark />);
    // The FIRST gradient paints the trace; the second is the bright tail.
    const gradient = container.querySelectorAll("linearGradient")[0];
    return [...gradient!.querySelectorAll("stop")].map((stop) => ({
      offset: stop.getAttribute("offset"),
      color: stop.getAttribute("stop-color"),
    }));
  }

  it("runs horizontally, so the ramp follows the trace", () => {
    const { container } = render(<BrandMark />);
    const gradient = container.querySelectorAll("linearGradient")[0];
    expect(gradient).toHaveAttribute("x1", "0%");
    expect(gradient).toHaveAttribute("x2", "100%");
    expect(gradient).toHaveAttribute("y1", "0%");
    expect(gradient).toHaveAttribute("y2", "0%");
  });

  it("has exactly the five specified stops, in order", () => {
    expect(stops()).toEqual([
      { offset: "0%", color: "#39248F" },
      { offset: "36%", color: "#5B3FD6" },
      { offset: "90%", color: "#8B78E8" },
      { offset: "94%", color: "#FFFFFF" },
      { offset: "100%", color: "#FFFFFF" },
    ]);
  });

  it("confines white to 94% and 100% and nowhere else", () => {
    // Any earlier and the whole right-hand baseline washes out, losing the
    // "the pen just passed here" read that makes the endpoint the brightest
    // thing in the mark.
    const white = stops().filter((stop) => stop.color?.toUpperCase() === "#FFFFFF");
    expect(white.map((stop) => stop.offset)).toEqual(["94%", "100%"]);
  });

  it("keeps the white section within the final 6% of the line", () => {
    const white = stops().filter((stop) => stop.color?.toUpperCase() === "#FFFFFF");
    const first = Number(white[0]?.offset?.replace("%", ""));
    expect(100 - first).toBeLessThanOrEqual(6);
  });
});

describe("the glow", () => {
  function filterOf(container: HTMLElement) {
    const filter = container.querySelector("filter");
    if (!filter) throw new Error("no filter element");
    return filter;
  }

  it("is given room so the blur is not clipped into a hard edge", () => {
    const { container } = render(<BrandMark />);
    const filter = filterOf(container);
    expect(filter).toHaveAttribute("x", "-24%");
    expect(filter).toHaveAttribute("y", "-44%");
    expect(filter).toHaveAttribute("width", "148%");
    expect(filter).toHaveAttribute("height", "188%");
  });

  it("blurs in sRGB, matching the space the stops were chosen in", () => {
    const { container } = render(<BrandMark />);
    expect(filterOf(container)).toHaveAttribute("color-interpolation-filters", "sRGB");
  });

  it("layers a wide blur at 4 and a tight blur at 1.4", () => {
    // One blur alone reads as a smudge; several read as light. Scoped to the
    // trace's own filter — the endpoint bloom is a separate one.
    const { container } = render(<BrandMark />);
    const deviations = [...filterOf(container).querySelectorAll("feGaussianBlur")].map((node) =>
      node.getAttribute("stdDeviation"),
    );
    expect(deviations).toEqual(["4", "1.4"]);
  });

  it("merges widest first and the crisp source last", () => {
    // Order decides whether the line stays sharp or is buried under its halo.
    const { container } = render(<BrandMark />);
    const order = [...filterOf(container).querySelectorAll("feMergeNode")].map((node) =>
      node.getAttribute("in"),
    );
    expect(order[0]).toBe("glowWide");
    expect(order.at(-1)).toBe("SourceGraphic");
    expect(order).toContain("glowTight");
  });
});

describe("the bright tail", () => {
  it("is the same line, not an object placed on it", () => {
    const { container } = render(<BrandMark />);
    const [trace, highlight] = [...container.querySelectorAll("polyline")];
    // Identical geometry, width and caps — it IS the trace, painted twice.
    expect(highlight).toHaveAttribute("points", trace!.getAttribute("points")!);
    expect(highlight).toHaveAttribute("stroke-width", "4");
    expect(highlight).toHaveAttribute("stroke-linecap", "round");
    expect(highlight).toHaveAttribute("fill", "none");
  });

  it("carries no circle, dot or other added shape", () => {
    const { container } = render(<BrandMark />);
    expect(container.querySelector("circle")).toBeNull();
    expect(container.querySelector("ellipse")).toBeNull();
    expect(container.querySelector("rect")).toBeNull();
  });

  it("is invisible until the final stretch, then white", () => {
    const { container } = render(<BrandMark />);
    const gradient = container.querySelectorAll("linearGradient")[1];
    const stops = [...gradient!.querySelectorAll("stop")].map((stop) => ({
      offset: stop.getAttribute("offset"),
      opacity: stop.getAttribute("stop-opacity"),
      color: stop.getAttribute("stop-color"),
    }));
    expect(stops).toEqual([
      { offset: "0%", opacity: "0", color: "#FFFFFF" },
      { offset: "88%", opacity: "0", color: "#FFFFFF" },
      { offset: "94%", opacity: "1", color: "#FFFFFF" },
      { offset: "100%", opacity: "1", color: "#FFFFFF" },
    ]);
  });

  it("is confined to a small final part of the line", () => {
    const { container } = render(<BrandMark />);
    const gradient = container.querySelectorAll("linearGradient")[1];
    const opaque = [...gradient!.querySelectorAll("stop")].filter(
      (stop) => stop.getAttribute("stop-opacity") === "1",
    );
    const first = Number(opaque[0]?.getAttribute("offset")?.replace("%", ""));
    // Fully white only over the last 6%, and fading in from 88%.
    expect(100 - first).toBeLessThanOrEqual(6);
  });

  it("blooms wider than the trace does", () => {
    const { container } = render(<BrandMark />);
    const bloom = [...container.querySelectorAll("filter")][1];
    const deviations = [...bloom!.querySelectorAll("feGaussianBlur")].map((node) =>
      Number(node.getAttribute("stdDeviation")),
    );
    expect(deviations).toEqual([7, 2.6]);
    expect(Math.max(...deviations)).toBeGreaterThan(4);
  });

  it("draws in step with the trace rather than fading in afterwards", () => {
    const { container } = render(<BrandMark />);
    const lines = [...container.querySelectorAll("polyline")];
    // Same animation class, so the tail arrives exactly as the line lands.
    for (const line of lines) expect(line).toHaveClass("brandmark-trace");
  });
});

describe("the tile", () => {
  it.each([
    ["compact", 44, 12],
    ["hero", 210, 16],
  ] as const)("%s is %spx with a %spx radius", (size, box, radius) => {
    const { container } = render(<BrandMark size={size} />);
    const tile = container.firstElementChild as HTMLElement;
    expect(tile.style.width).toBe(`${box}px`);
    expect(tile.style.height).toBe(`${box}px`);
    expect(tile.style.borderRadius).toBe(`${radius}px`);
    expect(container.querySelector("svg")).toHaveAttribute("width", String(box));
  });

  it("has a transparent ground so the glow bleeds into the page", () => {
    const { container } = render(<BrandMark />);
    const tile = container.firstElementChild as HTMLElement;
    expect(tile.style.backgroundColor).toBe("transparent");
    // Not clipped: a clipped glow has a hard border exactly where it should be
    // softest.
    expect(tile.style.overflow).toBe("visible");
    expect(tile.style.border).toBe("");
  });
});

describe("the animation", () => {
  it("is declared to run exactly once, and to hold its end state", () => {
    const rule = CSS.slice(CSS.indexOf(".brandmark-trace {"));
    expect(rule).toMatch(/animation-iteration-count:\s*1\s*;/);
    expect(rule).toMatch(/animation-fill-mode:\s*forwards\s*;/);
    expect(rule).toMatch(/animation-duration:\s*1\.2s\s*;/);
    expect(rule).toMatch(/animation-delay:\s*180ms\s*;/);
    expect(rule).toMatch(/animation-timing-function:\s*cubic-bezier\(0\.4,\s*0,\s*0\.2,\s*1\)/);
    expect(rule).toMatch(/stroke-dasharray:\s*600\s*;/);
    expect(rule).toMatch(/stroke-dashoffset:\s*600\s*;/);
  });

  it("never loops", () => {
    expect(CSS).not.toMatch(/animation-iteration-count:\s*infinite/);
  });

  it("ramps opacity to full at 72% and holds to 100%", () => {
    const frames = CSS.slice(CSS.indexOf("@keyframes brandmark-draw"));
    expect(frames).toMatch(/0%\s*\{\s*opacity:\s*0\.48;\s*stroke-dashoffset:\s*600;/);
    expect(frames).toMatch(/72%\s*\{\s*opacity:\s*1;\s*stroke-dashoffset:\s*0;/);
    expect(frames).toMatch(/100%\s*\{\s*opacity:\s*1;\s*stroke-dashoffset:\s*0;/);
  });

  it("reduces duration and iteration count under prefers-reduced-motion", () => {
    const reduced = CSS.slice(CSS.indexOf("@media (prefers-reduced-motion: reduce)"));
    const rule = reduced.slice(reduced.indexOf(".brandmark-trace"));
    // Iteration count stays at 1 so `forwards` lands the finished state.
    expect(rule).toMatch(/animation-duration:\s*0\.01ms/);
    expect(rule).toMatch(/animation-iteration-count:\s*1/);
  });

  it("is applied to both strokes by default and omitted when asked", () => {
    const on = [...render(<BrandMark />).container.querySelectorAll("polyline")];
    expect(on).toHaveLength(2);
    for (const line of on) expect(line).toHaveClass("brandmark-trace");

    const off = [...render(<BrandMark animate={false} />).container.querySelectorAll("polyline")];
    for (const line of off) expect(line).not.toHaveClass("brandmark-trace");
  });
});

describe("the wordmark typing in", () => {
  it("wraps the hero name in the reveal, and only the hero", () => {
    const hero = render(<Wordmark size="hero" />).container;
    expect(hero.querySelector(".brandmark-type")).toHaveTextContent("Synapse");
    // The header wordmark does not re-type on every navigation.
    const compact = render(<Wordmark />).container;
    expect(compact.querySelector(".brandmark-type")).toBeNull();
  });

  it("leaves the name in the DOM in full from the first paint", () => {
    // The reveal is clip-path: it changes what is PAINTED, not what is there,
    // so a screen reader reads the name immediately wherever the animation is.
    render(<Wordmark size="hero" />);
    expect(screen.getByRole("heading", { level: 1, name: "Synapse" })).toBeInTheDocument();
  });

  it("is skipped when animation is turned off", () => {
    const { container } = render(<Wordmark size="hero" animate={false} />);
    expect(container.querySelector(".brandmark-type")).toBeNull();
    expect(screen.getByRole("heading", { level: 1, name: "Synapse" })).toBeInTheDocument();
  });

  it("reveals in steps, once, holding the end state", () => {
    const rule = CSS.slice(CSS.indexOf(".brandmark-type {"));
    expect(rule).toMatch(/animation-timing-function:\s*steps\(7,\s*end\)/);
    expect(rule).toMatch(/animation-iteration-count:\s*1\s*;/);
    expect(rule).toMatch(/animation-fill-mode:\s*forwards\s*;/);
    expect(rule).toMatch(/clip-path:\s*inset\(0 100% 0 0\)/);
  });

  it("begins on the same frame as the mark", () => {
    // One startup, not two events: the name and the trace share a delay.
    const typeRule = CSS.slice(CSS.indexOf(".brandmark-type {"));
    const traceRule = CSS.slice(CSS.indexOf(".brandmark-trace {"));
    const delayOf = (rule: string) => /animation-delay:\s*(\d+)ms/.exec(rule)?.[1];
    expect(delayOf(typeRule)).toBe(delayOf(traceRule));
    expect(delayOf(typeRule)).toBe("180");
  });

  it("finishes before the trace does", () => {
    // The name lands while the pulse is still drawing beneath it.
    const typeRule = CSS.slice(CSS.indexOf(".brandmark-type {"));
    const duration = Number(/animation-duration:\s*(\d+)ms/.exec(typeRule)?.[1]);
    expect(duration).toBeLessThan(1200);
  });

  it("paints the name in full under reduced motion", () => {
    const reduced = CSS.slice(CSS.indexOf("@media (prefers-reduced-motion: reduce)"));
    expect(reduced).toMatch(/\.brandmark-type\s*\{[^}]*clip-path:\s*inset\(0 0 0 0\)/);
  });
});

describe("assistive technology", () => {
  it("never announces the mark", () => {
    const { container } = render(<BrandMark />);
    const tile = container.firstElementChild as HTMLElement;
    const svg = container.querySelector("svg");
    expect(tile).toHaveAttribute("aria-hidden", "true");
    expect(svg).toHaveAttribute("aria-hidden", "true");
    expect(svg).toHaveAttribute("role", "presentation");
    // An SVG is focusable in some browsers unless told otherwise.
    expect(svg).toHaveAttribute("focusable", "false");
  });
});

describe("the wordmark", () => {
  it("names the product in the hero, with the attribution beneath it", () => {
    render(<Wordmark size="hero" />);
    expect(screen.getByText("Synapse")).toBeInTheDocument();
    expect(screen.getByText("A Zenith Company")).toBeInTheDocument();
  });

  it("drops the attribution in the compact header", () => {
    // It already appears in the hero and again in the footer.
    render(<Wordmark />);
    expect(screen.getByText("Synapse")).toBeInTheDocument();
    expect(screen.queryByText("A Zenith Company")).toBeNull();
  });

  it("sets the attribution as an attribution, not a claim", () => {
    render(<Wordmark size="hero" />);
    expect(screen.getByText("A Zenith Company")).toHaveClass("attribution");
  });

  it("sets the name in the display serif at hero size", () => {
    render(<Wordmark size="hero" />);
    expect(screen.getByRole("heading", { level: 1, name: "Synapse" })).toHaveClass(
      "font-display",
    );
  });

  it("uses the hero tile in the hero and the compact tile in the header", () => {
    const hero = render(<Wordmark size="hero" />).container.querySelector("svg");
    expect(hero).toHaveAttribute("width", "210");
    const compact = render(<Wordmark />).container.querySelector("svg");
    expect(compact).toHaveAttribute("width", "44");
  });

  it("puts the mark ABOVE the name in the hero arrangement", () => {
    const { container } = render(<Wordmark size="hero" />);
    const svg = container.querySelector("svg");
    const heading = container.querySelector("h1");
    expect(svg).not.toBeNull();
    expect(heading).not.toBeNull();
    expect(
      svg!.compareDocumentPosition(heading!) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("links home with an accessible name when a destination is given", () => {
    render(<Wordmark href="/" />);
    expect(screen.getByRole("link", { name: /Synapse.*home/i })).toHaveAttribute("href", "/");
  });

  it("is not a link on the access screen, where there is no home yet", () => {
    render(<Wordmark />);
    expect(screen.queryByRole("link")).toBeNull();
  });
});
