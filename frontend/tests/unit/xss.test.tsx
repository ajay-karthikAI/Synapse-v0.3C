import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TurnView } from "@/components/answer/TurnView";
import type { AnswerEnvelope } from "@/lib/envelope";
import { EXTERNAL_LINK_REL, safeExternalUrl } from "@/lib/url";

import { envelope } from "../fixtures/load";

/**
 * Injection, from both directions.
 *
 * Two different defences are tested here, because they protect against two
 * different things and only one of them is React's doing.
 *
 * **Text.** React escapes interpolated strings, so markup in a title or a claim
 * is displayed rather than parsed. That holds *only* while nothing uses
 * `dangerouslySetInnerHTML`, so its absence is asserted across the whole source
 * tree rather than trusted.
 *
 * **Attributes.** React does not escape `href`. A `javascript:` URL in an
 * anchor executes on click, with a development-only warning and no runtime
 * protection at all. That is what `safeExternalUrl` is for, and the payloads
 * below are the ones that defeat a regular expression written over the raw
 * string.
 */

const HOSTILE_URLS = [
  "javascript:alert(1)",
  "JaVaScRiPt:alert(1)",
  // Control characters inside the scheme: stripped by the URL parser, so a
  // regex over the raw text sees "java\nscript:" and lets it through.
  "java\nscript:alert(1)",
  "java\tscript:alert(1)",
  "javascript:alert(1)",
  "  javascript:alert(1)  ",
  "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
  "vbscript:msgbox(1)",
  "file:///etc/passwd",
  "blob:https://example.com/uuid",
  "about:blank",
  // Not a URL at all.
  "not a url",
  "",
  "   ",
];

describe("URLs that must never reach an href", () => {
  it.each(HOSTILE_URLS)("rejects %j", (raw) => {
    expect(safeExternalUrl(raw)).toBeNull();
  });

  it("accepts ordinary http and https", () => {
    expect(safeExternalUrl("https://pubmed.ncbi.nlm.nih.gov/41802233/")).toBe(
      "https://pubmed.ncbi.nlm.nih.gov/41802233/",
    );
    expect(safeExternalUrl("http://example.org/a?b=c#d")).toBe("http://example.org/a?b=c#d");
  });

  it("normalises what it returns rather than echoing the input", () => {
    // Re-serialised from the parse, so encoding tricks that survived the
    // constructor do not survive the return.
    expect(safeExternalUrl("https://EXAMPLE.org/a")).toBe("https://example.org/a");
  });

  it("rejects null and undefined without throwing", () => {
    expect(safeExternalUrl(null)).toBeNull();
    expect(safeExternalUrl(undefined)).toBeNull();
  });
});

describe("hostile content in a rendered answer", () => {
  /** The answer fixture, with attacker-controlled strings in every text field. */
  function poisoned(): AnswerEnvelope {
    const base = envelope("answer") as AnswerEnvelope;
    const payload = '<img src=x onerror="alert(1)">';
    return {
      ...base,
      summary: `${payload} summary`,
      resolved_query: `<script>alert("rewrite")</script>`,
      claims: base.claims.map((claim) => ({ ...claim, text: `${payload} ${claim.text}` })),
      questions_for_doctor: [`${payload} question`],
      limitations: [`${payload} limitation`],
      excerpts: base.excerpts.map((excerpt) => ({ ...excerpt, quote: `${payload} quote` })),
      sources: base.sources.map((source) => ({
        ...source,
        title: `${payload} title`,
        url: "javascript:alert(1)",
      })),
    };
  }

  it("renders markup as visible text and never as elements", () => {
    const { container } = render(
      <TurnView envelope={poisoned()} question="q" turnKey="t0" autoFocus={false} />,
    );
    // Nothing was parsed into the DOM...
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    // ...and the payload is on screen as text, which is what "escaped" means.
    expect(container.textContent).toContain('<img src=x onerror="alert(1)">');
  });

  it("drops a hostile source URL and still shows the title as text", () => {
    const { container } = render(
      <TurnView envelope={poisoned()} question="q" turnKey="t0" autoFocus={false} />,
    );
    for (const anchor of container.querySelectorAll("a")) {
      const href = anchor.getAttribute("href") ?? "";
      expect(href.toLowerCase()).not.toContain("javascript:");
      expect(href.toLowerCase()).not.toContain("data:");
    }
    // The citation survives as text: a source whose link failed validation is
    // still a source, and silently deleting it would misrepresent the answer.
    expect(screen.getAllByText(/title$/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/No link recorded/)).toHaveLength(2);
  });

  it("creates no event-handler attribute anywhere in the tree", () => {
    const { container } = render(
      <TurnView envelope={poisoned()} question="q" turnKey="t0" autoFocus={false} />,
    );
    // Asserted over real ATTRIBUTES, not over serialised HTML: the escaped
    // payload legitimately contains the characters ` onerror=` as visible
    // text, and a string search would match that and prove nothing.
    for (const element of container.querySelectorAll("*")) {
      const handlers = [...element.attributes].filter((attribute) =>
        attribute.name.toLowerCase().startsWith("on"),
      );
      expect(handlers.map((attribute) => attribute.name)).toEqual([]);
    }
  });

  it("escapes a hostile question typed by the patient", () => {
    const { container } = render(
      <TurnView
        envelope={envelope("answer")}
        question='<script>alert("q")</script>'
        turnKey="t0"
        autoFocus={false}
      />,
    );
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain('<script>alert("q")</script>');
  });
});

describe("every external link carries the full rel set", () => {
  it("uses noopener, noreferrer and nofollow", () => {
    const { container } = render(
      <TurnView envelope={envelope("answer")} question="q" turnKey="t0" autoFocus={false} />,
    );
    const external = [...container.querySelectorAll<HTMLAnchorElement>("a[href^='http']")];
    expect(external.length).toBeGreaterThan(0);
    for (const anchor of external) {
      // noopener: denies the opened page a handle back to this one.
      // noreferrer: withholds which Synapse page linked out.
      // nofollow: a retrieved citation is not an endorsement.
      expect(anchor.getAttribute("rel")).toBe(EXTERNAL_LINK_REL);
      expect(anchor.getAttribute("target")).toBe("_blank");
    }
  });

  it("tells a screen reader that the link opens a new tab", () => {
    render(<TurnView envelope={envelope("answer")} question="q" turnKey="t0" autoFocus={false} />);
    expect(screen.getAllByText(/opens in a new tab/).length).toBeGreaterThan(0);
  });
});

describe("the escaping guarantee holds across the source tree", () => {
  function sourceFiles(dir: string): string[] {
    return readdirSync(dir).flatMap((entry) => {
      const full = join(dir, entry);
      if (statSync(full).isDirectory()) return sourceFiles(full);
      return /\.tsx?$/.test(entry) ? [full] : [];
    });
  }

  /**
   * Source with comments removed.
   *
   * Required, not tidiness: these files DISCUSS the calls being banned, in
   * their own documentation, and a scan over raw text matches that prose and
   * reports the file that explains the rule as the file that breaks it.
   */
  function code(file: string): string {
    return readFileSync(file, "utf8")
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/^\s*\/\/.*$/gm, "");
  }

  it("uses dangerouslySetInnerHTML nowhere", () => {
    // React's escaping is the entire defence for dynamic text. One use of this
    // anywhere would make every argument above conditional.
    const offenders = sourceFiles(join(process.cwd(), "src")).filter((file) =>
      code(file).includes("dangerouslySetInnerHTML"),
    );
    expect(offenders).toEqual([]);
  });

  it("never assigns innerHTML or uses eval", () => {
    const offenders = sourceFiles(join(process.cwd(), "src")).filter((file) => {
      const body = code(file);
      return /\.innerHTML\s*=/.test(body) || /\beval\(/.test(body);
    });
    expect(offenders).toEqual([]);
  });

  it("would catch a real use, so the comment-stripping did not defeat it", () => {
    // Guards the guard: if `code()` stripped too much, every scan above passes
    // vacuously and nobody finds out.
    const stripped = code(join(process.cwd(), "src", "components", "answer", "AnswerCard.tsx"));
    expect(stripped).toContain("AnswerCard");
    expect(stripped).not.toContain("dangerouslySetInnerHTML");
  });
});
