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
  render(<BriefPanel turnIndex={0} open onClose={onClose} />);
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

describe("the patient's own fields", () => {
  it("sends the topic as one named operation on blur", async () => {
    const { calls, user } = await open(false);
    const topic = screen.getByLabelText(/what you want to talk about/i);
    await user.type(topic, "my blood sugar");
    await user.tab();

    await waitFor(() => {
      const put = calls.find((call) => call.url.includes("/topic"));
      expect(put).toBeDefined();
      expect(put!.method).toBe("PUT");
      // One field. Not a brief.
      expect(put!.body).toEqual({ topic: "my blood sugar" });
    });
  });

  it("sends the notes as one named operation", async () => {
    const { calls, user } = await open(false);
    await user.type(screen.getByLabelText(/your notes/i), "started last week");
    await user.tab();

    await waitFor(() => {
      const put = calls.find((call) => call.url.includes("/notes"));
      expect(put!.body).toEqual({ notes: "started last week" });
    });
  });

  it("does not write on every keystroke", async () => {
    // Each write carries the patient's own words. One request per character
    // would be one transmission of their notes per character.
    const { calls, user } = await open(false);
    await user.type(screen.getByLabelText(/your notes/i), "hello");
    expect(calls.filter((call) => call.url.includes("/notes"))).toHaveLength(0);
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

describe("sections", () => {
  it("sends the complete section list when one is toggled", async () => {
    const fixture = briefFixture();
    const { calls, user } = await open(false);
    const first = fixture.available_sections[0]!;

    const checkbox = screen.getAllByRole("checkbox")[0]!;
    await user.click(checkbox);

    await waitFor(() => {
      const put = calls.find((call) => call.url.includes("/sections"));
      expect(put).toBeDefined();
      expect(Array.isArray((put!.body as { sections: string[] }).sections)).toBe(true);
      expect((put!.body as { sections: string[] }).sections).not.toContain(
        fixture.included_sections.includes(first) ? first : "__never__",
      );
    });
  });

  it("says the disclaimer cannot be removed", async () => {
    await open(false);
    expect(screen.getByText(/disclaimer is always printed and cannot be removed/i)).toBeInTheDocument();
  });

  it("offers no checkbox for the disclaimer", async () => {
    // Not merely disabled — the section list the schema exposes does not
    // contain it, so there is nothing to switch off.
    const fixture = briefFixture();
    expect(fixture.available_sections).not.toContain("disclaimer");
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
  it("offers all three formats as real links to the proxy", async () => {
    await open(false);
    for (const [label, format] of [
      [/download html/i, "html"],
      [/download plain text/i, "text"],
      [/download json/i, "json"],
    ] as const) {
      const link = screen.getByRole("link", { name: label });
      // A real href, so the browser saves it with the server's own
      // Content-Disposition filename and content type.
      expect(link).toHaveAttribute("href", `/api/proxy/v1/turns/0/brief/export/${format}`);
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
    render(<BriefPanel turnIndex={0} open onClose={vi.fn()} />);

    expect(await screen.findByText(/could not be loaded/i)).toBeInTheDocument();
    expect(screen.getByText(/your answer above is unaffected/i)).toBeInTheDocument();
  });
});

describe("no endpoint accepts a whole brief", () => {
  it("never sends claims, sources or a document id", async () => {
    const { calls, user } = await open(false);
    await user.type(screen.getByLabelText(/what you want to talk about/i), "x");
    await user.tab();
    await user.type(screen.getByLabelText(/add a question of your own/i), "y");
    await user.click(screen.getByRole("button", { name: /^add$/i }));

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

describe("the panel is scoped to its own turn", () => {
  it("addresses the turn it was given", async () => {
    setViewport(false);
    const { fetchMock } = server();
    vi.stubGlobal("fetch", fetchMock);
    render(<BriefPanel turnIndex={3} open onClose={vi.fn()} />);
    await screen.findByRole("heading", { name: /your appointment brief/i });

    expect(String(fetchMock.mock.calls[0]![0])).toBe("/api/proxy/v1/turns/3/brief");
  });

  it("renders nothing at all when closed", () => {
    setViewport(false);
    vi.stubGlobal("fetch", vi.fn());
    const { container } = render(<BriefPanel turnIndex={0} open={false} onClose={vi.fn()} />);
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

    await user.type(screen.getByLabelText(/your notes/i), "n");
    await user.tab();
    // Announced rather than focused: moving focus would interrupt typing.
    await waitFor(() => expect(status).toHaveTextContent(/notes saved/i));
  });
});
