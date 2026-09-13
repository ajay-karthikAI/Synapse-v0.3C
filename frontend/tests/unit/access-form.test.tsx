import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AccessForm } from "@/components/access/AccessForm";

/**
 * The passcode form.
 *
 * The properties that matter are about what it does NOT do: it does not
 * distinguish a wrong passcode from any other refusal, it does not keep the
 * passcode after submitting, and it does not put it anywhere a URL or a log
 * could pick it up.
 *
 * The one exception is the lockout, which a patient must be told about because
 * the remedy is to wait rather than to retry.
 */

const router = vi.hoisted(() => ({ replace: vi.fn(), refresh: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => router,
}));

beforeEach(() => {
  router.replace.mockReset();
  router.refresh.mockReset();
});

function mockFetch(response: Partial<Response> & { json?: () => Promise<unknown> }) {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: response.ok ?? true,
    status: response.status ?? 200,
    json: response.json ?? (async () => ({ next: "/" })),
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("the field", () => {
  it("has a persistent visible label, not a placeholder", () => {
    // The previous interface used a placeholder as the label, which vanishes on
    // focus and is not reliably announced (accessibility.md §1).
    render(<AccessForm />);
    const input = screen.getByLabelText("Access passcode");
    expect(input).toBeInTheDocument();
    expect(input).not.toHaveAttribute("placeholder");
  });

  it("is a password field that no autofill or spellchecker touches", () => {
    render(<AccessForm />);
    const input = screen.getByLabelText("Access passcode");
    expect(input).toHaveAttribute("type", "password");
    expect(input).toHaveAttribute("autocomplete", "off");
    expect(input).toHaveAttribute("spellcheck", "false");
  });

  it("describes where the passcode comes from", () => {
    render(<AccessForm />);
    expect(screen.getByLabelText("Access passcode")).toHaveAccessibleDescription(
      /provided by whoever set up this demonstration/i,
    );
  });
});

describe("the live region", () => {
  it("exists before it has anything to say", () => {
    // A region created at the moment its text arrives is often not announced —
    // the cause of the previous interface's unreliable announcements
    // (accessibility.md §6, limitation 4).
    render(<AccessForm />);
    const region = document.getElementById("passcode-error");
    expect(region).not.toBeNull();
    expect(region).toHaveAttribute("role", "status");
    expect(region).toHaveAttribute("aria-live", "polite");
  });

  it("is polite, never assertive", () => {
    // Emergency is the only state in this application that may interrupt.
    render(<AccessForm />);
    expect(document.getElementById("passcode-error")).toHaveAttribute("aria-live", "polite");
  });
});

describe("submitting", () => {
  it("sends the passcode to this server, not to the backend", async () => {
    const fetchMock = mockFetch({ ok: true });
    const user = userEvent.setup();
    render(<AccessForm />);

    await user.type(screen.getByLabelText("Access passcode"), "open-sesame");
    await user.click(screen.getByRole("button", { name: /continue/i }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/access/login");
    expect(init.method).toBe("POST");
    // In the body, never in the URL: a query string reaches server logs,
    // browser history and the Referer header.
    expect(url).not.toContain("open-sesame");
    expect(String(init.body)).toContain("open-sesame");
  });

  it("replaces the history entry so back does not return to the passcode", async () => {
    mockFetch({ ok: true, json: async () => ({ next: "/" }) });
    const user = userEvent.setup();
    render(<AccessForm />);

    await user.type(screen.getByLabelText("Access passcode"), "open-sesame");
    await user.click(screen.getByRole("button", { name: /continue/i }));

    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/"));
    expect(router.refresh).toHaveBeenCalled();
  });

  it("clears the passcode from state afterwards", async () => {
    mockFetch({ ok: true });
    const user = userEvent.setup();
    render(<AccessForm />);

    const input = screen.getByLabelText("Access passcode");
    await user.type(input, "open-sesame");
    await user.click(screen.getByRole("button", { name: /continue/i }));

    await waitFor(() => expect(input).toHaveValue(""));
  });

  it("cannot be submitted empty", () => {
    render(<AccessForm />);
    expect(screen.getByRole("button", { name: /continue/i })).toBeDisabled();
  });
});

describe("refusals", () => {
  it("shows one generic message for a wrong passcode", async () => {
    mockFetch({ ok: false, status: 401 });
    const user = userEvent.setup();
    render(<AccessForm />);

    await user.type(screen.getByLabelText("Access passcode"), "wrong");
    await user.click(screen.getByRole("button", { name: /continue/i }));

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("Wrong Password. Please try again or contact the front desk."),
    );
  });

  it("shows the same message for a server fault", async () => {
    // A caller must not be able to tell "wrong passcode" from "backend down".
    mockFetch({ ok: false, status: 503 });
    const user = userEvent.setup();
    render(<AccessForm />);

    await user.type(screen.getByLabelText("Access passcode"), "anything");
    await user.click(screen.getByRole("button", { name: /continue/i }));

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("Wrong Password. Please try again or contact the front desk."),
    );
  });

  it("tells the patient about a lockout, because the remedy is different", async () => {
    mockFetch({ ok: false, status: 429 });
    const user = userEvent.setup();
    render(<AccessForm />);

    await user.type(screen.getByLabelText("Access passcode"), "anything");
    await user.click(screen.getByRole("button", { name: /continue/i }));

    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/wait a few minutes/i));
  });

  it("marks the field invalid and points to the message", async () => {
    mockFetch({ ok: false, status: 401 });
    const user = userEvent.setup();
    render(<AccessForm />);

    const input = screen.getByLabelText("Access passcode");
    await user.type(input, "wrong");
    await user.click(screen.getByRole("button", { name: /continue/i }));

    await waitFor(() => expect(input).toHaveAttribute("aria-invalid", "true"));
    expect(input).toHaveAttribute("aria-errormessage", "passcode-error");
  });

  it("survives a network failure without leaking anything", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("ECONNREFUSED 10.0.0.5:8000")));
    const user = userEvent.setup();
    render(<AccessForm />);

    await user.type(screen.getByLabelText("Access passcode"), "anything");
    await user.click(screen.getByRole("button", { name: /continue/i }));

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("Wrong Password. Please try again or contact the front desk."),
    );
    // The exception's text names an internal address. It must not reach the page.
    expect(document.body.textContent).not.toContain("ECONNREFUSED");
    expect(document.body.textContent).not.toContain("10.0.0.5");
  });

  it("does not navigate on any refusal", async () => {
    mockFetch({ ok: false, status: 401 });
    const user = userEvent.setup();
    render(<AccessForm />);

    await user.type(screen.getByLabelText("Access passcode"), "wrong");
    await user.click(screen.getByRole("button", { name: /continue/i }));

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        "Wrong Password. Please try again or contact the front desk.",
      ),
    );
    expect(router.replace).not.toHaveBeenCalled();
  });
});

describe("the return destination", () => {
  it("is carried through a sign-in", async () => {
    mockFetch({ ok: true, json: async () => ({ next: "/transparency" }) });
    const user = userEvent.setup();
    render(<AccessForm next="/transparency" />);

    await user.type(screen.getByLabelText("Access passcode"), "open-sesame");
    await user.click(screen.getByRole("button", { name: /continue/i }));

    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/transparency"));
  });

  it("comes from the server's response, never from the form's own input", async () => {
    // The server re-validates `next`; whatever it returns is what is used, so a
    // manipulated client value cannot become an open redirect.
    mockFetch({ ok: true, json: async () => ({ next: "/" }) });
    const user = userEvent.setup();
    render(<AccessForm next="https://evil.example" />);

    await user.type(screen.getByLabelText("Access passcode"), "open-sesame");
    await user.click(screen.getByRole("button", { name: /continue/i }));

    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/"));
  });
});
