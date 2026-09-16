import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BriefPanel } from "@/components/brief/BriefPanel";
import type { Brief } from "@/lib/envelope";

import { brief as briefFixture } from "../fixtures/load";

/**
 * The appointment brief, in both of its layouts.
 *
 * The same content is a **non-modal side panel** on a wide screen and a
 * **modal dialog** on a narrow one, and the difference is semantic rather than
 * cosmetic: a full-screen overlay that does not trap focus strands a
 * screen-reader user underneath it, and a side panel that does trap focus makes
 * the rest of the page unreachable. Both are tested, because a single-layout
 * test would pass while the other layout was broken.
 *
 * The other property under test is that **no route sends a whole brief**. Each
 * edit is one named operation on one field; anything else would let a caller
 * put unverified claims on a page a patient hands to a clinician.
 */

/** Make `matchMedia` report a viewport width, as the setup file's stub cannot. */
function setViewport(desktop: boolean) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: desktop && query.includes("min-width: 1024px"),
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

function json(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    headers: { "content-type": "application/json" },
  });
}

/** Records every request, and answers each with the (optionally edited) brief. */
function server(overrides: Partial<Brief> = {}) {
  const calls: { url: string; method: string; body: unknown }[] = [];
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ url: String(input), method: init?.method ?? "GET", body });
    return Promise.resolve(json({ ...briefFixture(), ...overrides }));
  });
  return { calls, fetchMock };
}

beforeEach(() => setViewport(false));
afterEach(() => vi.unstubAllGlobals());

async function open(desktop: boolean, overrides: Partial<Brief> = {}) {
  setViewport(desktop);
  const { calls, fetchMock } = server(overrides);
  vi.stubGlobal("fetch", fetchMock);
  const onClose = vi.fn();
  const user = userEvent.setup();
  render(<BriefPanel open onClose={onClose} />);
  await screen.findByRole("heading", { name: /your appointment brief/i });
  return { calls, fetchMock, onClose, user };
}

describe("mobile: a modal dialog", () => {
  it("is a dialog, and declares itself modal", async () => {
    await open(false);
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveAccessibleName(/your appointment brief/i);
  });

  it("takes focus when it opens", async () => {
    await open(false);
    expect(screen.getByRole("dialog")).toHaveFocus();
  });

  it("closes on Escape", async () => {
    const { onClose, user } = await open(false);
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalled();
  });

  it("traps Tab inside itself", async () => {
    // Without this, Tab walks out of a full-screen overlay into a page the
    // user cannot see.
    const { user } = await open(false);
    const dialog = screen.getByRole("dialog");
    const focusable = dialog.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled])',
    );
    const last = focusable[focusable.length - 1]!;
    last.focus();
    await user.tab();
    expect(dialog.contains(document.activeElement)).toBe(true);
  });
});

describe("desktop: a side panel", () => {
  it("is a complementary region, not a dialog", async () => {
    await open(true);
    // It does not cover the page, so claiming to be modal would be a lie that
    // hides the rest of the document from assistive technology.
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByRole("complementary")).toBeInTheDocument();
  });

  it("still closes on Escape", async () => {
    const { onClose, user } = await open(true);
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalled();
  });

  it("does not trap Tab", async () => {
    const { user } = await open(true);
    const panel = screen.getByRole("complementary");
    const focusable = panel.querySelectorAll<HTMLElement>("a[href], button:not([disabled])");
    focusable[focusable.length - 1]!.focus();
    await user.tab();
    // Focus is free to leave: the rest of the page is still operable.
    expect(panel.contains(document.activeElement)).toBe(false);
  });

  it("renders no scrim", async () => {
    await open(true);
    expect(document.querySelector(".fixed.inset-0.bg-black\\/60")).toBeNull();
  });
});

describe("the panel asks for one thing", () => {
  /**
   * It used to open with a "what you want to talk about" line and a blank
   * notes box. Both are gone: a patient whose question has just been answered
   * was being asked to type the question in again, and the notes box was an
   * empty page with no prompt. The contract still accepts both fields — the
   * panel simply does not offer them.
   */
  it("offers no topic or notes field", async () => {
    await open(false);
    expect(screen.queryByLabelText(/what you want to talk about/i)).toBeNull();
    expect(screen.queryByLabelText(/your notes/i)).toBeNull();
  });

  it("never writes a topic or a note", async () => {
    const { calls, user } = await open(false);
    await user.type(screen.getByLabelText(/add a question of your own/i), "Is this normal?");
    await user.click(screen.getByRole("button", { name: /^add$/i }));

    await waitFor(() => expect(calls.some((call) => call.url.includes("/questions"))).toBe(true));
    expect(calls.filter((call) => call.url.includes("/topic"))).toHaveLength(0);
    expect(calls.filter((call) => call.url.includes("/notes"))).toHaveLength(0);
  });

  it("asks whether the patient wants to add their own questions", async () => {
    await open(false);
    expect(
      screen.getByRole("heading", {
        name: /would you like to add more questions for your physician\?/i,
      }),
    ).toBeInTheDocument();
  });

  it("keeps an accessible name on the input the heading sits above", async () => {
    // A heading is not a label: without this the box announces nothing to a
    // screen reader arriving by Tab.
    await open(false);
    expect(screen.getByLabelText(/add a question of your own/i)).toBeInTheDocument();
  });
});

describe("questions: add, remove, reorder", () => {
  it("adds a question the patient wrote", async () => {
    const { calls, user } = await open(false);
    await user.type(screen.getByLabelText(/add a question of your own/i), "Is this normal?");
    await user.click(screen.getByRole("button", { name: /^add$/i }));

    await waitFor(() => {
      const post = calls.find((call) => call.method === "POST");
      expect(post!.url).toMatch(/\/questions$/);
      expect(post!.body).toEqual({ text: "Is this normal?" });
    });
  });

  it("removes one by identifier", async () => {
    const fixture = briefFixture();
    const first = fixture.questions[0]!;
    const { calls, user } = await open(false);
    await user.click(screen.getByRole("button", { name: `Remove "${first.text}"` }));

    await waitFor(() => {
      const del = calls.find((call) => call.method === "DELETE");
      expect(del!.url).toContain(encodeURIComponent(first.question_id));
    });
  });

  it("reorders by sending the full identifier order", async () => {
    const fixture = briefFixture();
    const ids = fixture.questions.map((question) => question.question_id);
    const { calls, user } = await open(false);

    await user.click(screen.getByRole("button", { name: `Move "${fixture.questions[1]!.text}" up` }));

    await waitFor(() => {
      const put = calls.find((call) => call.url.includes("/questions/order"));
      expect(put!.body).toEqual({ order: [ids[1], ids[0], ...ids.slice(2)] });
    });
  });

  it("uses buttons rather than dragging, so reordering works from a keyboard", async () => {
    const fixture = briefFixture();
    await open(false);
    // Every move control is a real button with a sentence for a name.
    const up = screen.getByRole("button", { name: `Move "${fixture.questions[1]!.text}" up` });
    expect(up.tagName).toBe("BUTTON");
    expect(document.querySelector("[draggable='true']")).toBeNull();
  });

  it("disables the move that would go off the end of the list", async () => {
    const fixture = briefFixture();
    await open(false);
    expect(
      screen.getByRole("button", { name: `Move "${fixture.questions[0]!.text}" up` }),
    ).toBeDisabled();
    const last = fixture.questions[fixture.questions.length - 1]!;
    expect(screen.getByRole("button", { name: `Move "${last.text}" down` })).toBeDisabled();
  });

  it("marks which questions the patient wrote themselves", async () => {
    await open(false);
    // A question the patient added must not be mistaken for one drawn from
    // evidence, in the panel or on the printed page.
    const fixture = briefFixture();
    if (fixture.questions.some((question) => question.origin === "user")) {
      expect(screen.getAllByText(/\(yours\)/).length).toBeGreaterThan(0);
    }
  });
});

describe("add-ons", () => {
  /**
   * The panel used to list every section with every box ticked, which is how
   * the brief became a nine-section engineering document: a patient was asked
   * to decide about "claims", "sources" and "limitations" separately and had no
   * basis for any of those decisions. There are two choices now.
   */
  it("offers exactly two, and both start off", async () => {
    await open(false);
    const boxes = screen.getAllByRole("checkbox");
    expect(boxes).toHaveLength(2);
    for (const box of boxes) expect(box).not.toBeChecked();
  });

  it("names them in the patient's terms", async () => {
    await open(false);
    expect(screen.getByRole("checkbox", { name: /your conversation/i })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /the research behind it/i })).toBeInTheDocument();
  });

  it("turning the transcript on sends the defaults plus transcript", async () => {
    const fixture = briefFixture();
    const { calls, user } = await open(false);
    await user.click(screen.getByRole("checkbox", { name: /your conversation/i }));

    await waitFor(() => {
      const put = calls.find((call) => call.url.includes("/sections"));
      expect(put).toBeDefined();
      const sent = (put!.body as { sections: string[] }).sections;
      expect(sent).toContain("transcript");
      for (const section of fixture.default_sections) expect(sent).toContain(section);
    });
  });

  it("turning the research on sends every research section at once", async () => {
    const fixture = briefFixture();
    const { calls, user } = await open(false);
    await user.click(screen.getByRole("checkbox", { name: /the research behind it/i }));

    await waitFor(() => {
      const put = calls.find((call) => call.url.includes("/sections"));
      expect(put).toBeDefined();
      const sent = (put!.body as { sections: string[] }).sections;
      // Derived from the contract: everything available but not default, less
      // the transcript. Hard-coding the list here would let this file and the
      // server disagree about what "the research" is.
      const research = fixture.available_sections.filter(
        (section) => !fixture.default_sections.includes(section) && section !== "transcript",
      );
      expect(research.length).toBeGreaterThan(0);
      for (const section of research) expect(sent).toContain(section);
      expect(sent).not.toContain("transcript");
    });
  });

  it("says what is always included", async () => {
    await open(false);
    expect(
      screen.getByText(/summary, your questions and the disclaimer are always included/i),
    ).toBeInTheDocument();
  });

  it("offers no checkbox for the disclaimer", async () => {
    // Not merely disabled — the section list the schema exposes does not
    // contain it, so there is nothing to switch off.
    const fixture = briefFixture();
    expect(fixture.available_sections).not.toContain("disclaimer");
  });

  it("counts the conversation in the transcript description", async () => {
    await open(false, { transcript_turn_count: 3 });
    expect(screen.getByText(/all 3 questions you asked/i)).toBeInTheDocument();
  });
});

describe("overflow guidance", () => {
  it("gives concrete advice when the brief runs past one page", async () => {
    await open(false, {
      fits_one_page: false,
      overflow_advice: ["Remove two questions.", "Turn off the sources section."],
    });
    expect(screen.getByText(/longer than one page/i)).toBeInTheDocument();
    expect(screen.getByText("Remove two questions.")).toBeInTheDocument();
  });

  it("says nothing when it fits", async () => {
    await open(false, { fits_one_page: true, overflow_advice: [] });
    expect(screen.queryByText(/longer than one page/i)).toBeNull();
  });
});

describe("exports", () => {
  it("leads with the PDF, because that is what a phone can save", async () => {
    await open(false);
    const link = screen.getByRole("link", { name: /download the pdf/i });
    // A real href, so the browser saves it with the server's own
    // Content-Disposition filename and content type.
    expect(link).toHaveAttribute("href", "/api/proxy/v1/brief/export/pdf");
  });

  it("keeps the other three formats as secondary links", async () => {
    await open(false);
    for (const [label, format] of [
      [/a web page/i, "html"],
      [/plain text/i, "text"],
      [/structured data/i, "json"],
    ] as const) {
      const link = screen.getByRole("link", { name: label });
      expect(link).toHaveAttribute("href", `/api/proxy/v1/brief/export/${format}`);
    }
  });

  it("does not set a client-side download filename", async () => {
    await open(false);
    // The filename is the server's to choose; it builds one from the brief's
    // own document id.
    for (const link of screen.getAllByRole("link")) {
      expect(link).not.toHaveAttribute("download");
    }
  });

  it("shows the export warning and the disclaimer", async () => {
    const fixture = briefFixture();
    await open(false);
    expect(screen.getByText(fixture.export_warning)).toBeInTheDocument();
    expect(screen.getByText(fixture.disclaimer)).toBeInTheDocument();
  });
});

describe("failure", () => {
  it("says so without taking the answer down with it", async () => {
    setViewport(false);
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response("{}", { status: 500 }))));
    render(<BriefPanel open onClose={vi.fn()} />);

    expect(await screen.findByText(/could not be loaded/i)).toBeInTheDocument();
    expect(screen.getByText(/your answer above is unaffected/i)).toBeInTheDocument();
  });
});

describe("no endpoint accepts a whole brief", () => {
  it("never sends claims, sources or a document id", async () => {
    const { calls, user } = await open(false);
    await user.type(screen.getByLabelText(/add a question of your own/i), "y");
    await user.click(screen.getByRole("button", { name: /^add$/i }));
    await user.click(screen.getByRole("checkbox", { name: /your conversation/i }));

    await waitFor(() => expect(calls.filter((call) => call.body).length).toBeGreaterThanOrEqual(2));
    for (const call of calls) {
      if (!call.body) continue;
      const keys = Object.keys(call.body as object);
      // The forgery surface: claims carry verified support levels and source
      // numbers, and must come from the answer layer or not exist.
      expect(keys).not.toContain("claims");
      expect(keys).not.toContain("sources");
      expect(keys).not.toContain("document_id");
      expect(keys.length).toBe(1);
    }
  });
});

describe("the panel is scoped to the conversation", () => {
  it("reads the one session-wide brief", async () => {
    // There is no turn in the path any more. A patient asking four questions
    // used to end up with four documents, none of which was the sheet of paper
    // they needed.
    setViewport(false);
    const { fetchMock } = server();
    vi.stubGlobal("fetch", fetchMock);
    render(<BriefPanel open onClose={vi.fn()} />);
    await screen.findByRole("heading", { name: /your appointment brief/i });

    expect(String(fetchMock.mock.calls[0]![0])).toBe("/api/proxy/v1/brief");
  });

  it("renders nothing at all when closed", () => {
    setViewport(false);
    vi.stubGlobal("fetch", vi.fn());
    const { container } = render(<BriefPanel open={false} onClose={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
    expect(fetch).not.toHaveBeenCalled();
  });
});

describe("edits are announced without moving focus", () => {
  it("reports the change in a polite live region", async () => {
    const { user } = await open(false);
    const dialog = screen.getByRole("dialog");
    const status = within(dialog).getByRole("status");
    expect(status).toHaveAttribute("aria-live", "polite");

    await user.type(screen.getByLabelText(/add a question of your own/i), "n");
    await user.click(screen.getByRole("button", { name: /^add$/i }));
    // Announced rather than focused: moving focus would interrupt typing.
    await waitFor(() => expect(status).toHaveTextContent(/question added/i));
  });
});
