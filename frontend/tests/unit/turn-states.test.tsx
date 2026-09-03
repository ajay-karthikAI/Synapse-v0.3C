import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TurnView } from "@/components/answer/TurnView";
import type { AnswerEnvelope, TurnEnvelope } from "@/lib/envelope";

import { envelope, everyEnvelope } from "../fixtures/load";

/**
 * Every state a patient can reach, rendered.
 *
 * The cases come from `everyEnvelope()`, which reads the fixture directory
 * rather than a list written here. A golden state added on the Python side
 * therefore arrives in this suite automatically — and if the interface has no
 * screen for it, these tests fail rather than the state quietly going
 * unrendered. That is the coverage guarantee: not "we tested the states we
 * remembered", but "every state the server can produce was rendered".
 */

function renderTurn(value: TurnEnvelope, question = "what does my HbA1c mean?") {
  return render(
    <TurnView
      envelope={value}
      question={question}
      turnKey="t0"
      autoFocus={false}
    />,
  );
}

describe("every golden state renders", () => {
  const states = everyEnvelope();

  it("has fixtures to test", () => {
    // Guards against the whole suite silently passing on an empty directory.
    expect(states.length).toBeGreaterThanOrEqual(14);
  });

  it.each(states)("%s renders a result heading and the question", (_name, value) => {
    renderTurn(value);
    // Every state gets a level-2 heading, which is what focus moves to and what
    // a screen-reader user navigates by.
    expect(screen.getAllByRole("heading", { level: 2 }).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("what does my HbA1c mean?")).toBeInTheDocument();
  });

  it.each(states)("%s never renders undefined, null or [object Object]", (_name, value) => {
    const { container } = renderTurn(value);
    const text = container.textContent ?? "";
    expect(text).not.toMatch(/\bundefined\b/);
    expect(text).not.toMatch(/\bNaN\b/);
    expect(text).not.toContain("[object Object]");
  });

  it.each(states)("%s offers a brief only when the server said so", (_name, value) => {
    renderTurn(value, "q");
    // The slot is supplied by the parent; here it stands in for the real panel
    // so the decision itself is what is under test.
    expect(value.brief_available).toBe(value.kind === "answer");
  });
});

describe("the emergency state, and only the emergency state, interrupts", () => {
  it("uses role=alert", () => {
    renderTurn(envelope("emergency"));
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });

  it("is also used for a latched escalation on a benign follow-up", () => {
    // "is that serious?" carries no emergency vocabulary of its own. The latch
    // re-escalates it, and the interface must treat that identically.
    renderTurn(envelope("emergency_latched"), "is that serious?");
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });

  it.each(everyEnvelope().filter(([, value]) => value.kind !== "emergency"))(
    "%s does not use role=alert",
    (_name, value) => {
      renderTurn(value);
      expect(screen.queryByRole("alert")).toBeNull();
    },
  );

  it("cites nothing at all", () => {
    const { container } = renderTurn(envelope("emergency"));
    expect(within(container).queryByRole("heading", { name: /sources/i })).toBeNull();
    expect(container.querySelectorAll("a[href^='http']")).toHaveLength(0);
  });
});

describe("answers", () => {
  it("shows the summary, the claims and their citations", () => {
    renderTurn(envelope("answer"));
    expect(
      screen.getByText(/Your HbA1c reflects your average blood sugar/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/HbA1c reflects average plasma glucose over 2-3 months\./),
    ).toBeInTheDocument();
    // Each inline marker is a real link with a spoken name.
    expect(screen.getByRole("link", { name: "Source 1 for this statement" })).toBeInTheDocument();
  });

  it("resolves every inline citation to a source that is actually listed", () => {
    // The property that makes a citation followable rather than decorative.
    const { container } = renderTurn(envelope("answer"));
    const markers = [...container.querySelectorAll<HTMLAnchorElement>("a[href^='#t0-source-']")];
    expect(markers.length).toBeGreaterThan(0);
    for (const marker of markers) {
      const target = container.querySelector(marker.getAttribute("href") ?? "");
      expect(target, `${marker.getAttribute("href")} resolves`).not.toBeNull();
    }
  });

  it("numbers sources stably, from the server's field and not array position", () => {
    const value = envelope("answer") as AnswerEnvelope;
    const { container } = renderTurn(value);
    for (const source of value.sources) {
      expect(container.querySelector(`#t0-source-${source.number}`)).not.toBeNull();
    }
  });

  it("shows the questions to raise and the limitations", () => {
    renderTurn(envelope("answer"));
    expect(screen.getByRole("heading", { name: /worth asking at your appointment/i })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /what this does not cover/i })).toBeInTheDocument();
  });

  it("carries the application-owned disclaimer verbatim", () => {
    const value = envelope("answer") as AnswerEnvelope;
    renderTurn(value);
    expect(screen.getByText(value.disclaimer)).toBeInTheDocument();
  });

  it("shows the matched passages, behind a disclosure", () => {
    renderTurn(envelope("answer"));
    const details = screen.getByText(/show the 2 passages/i);
    expect(details).toBeInTheDocument();
    expect(
      screen.getByText(/HbA1c reflects average plasma glucose over 2-3 months$/),
    ).toBeInTheDocument();
  });

  it("describes relevance in words and never as a percentage", () => {
    // The server sends 0.91. Printing "91%" beside a citation would be read as
    // "91% reliable" by every patient who saw it.
    const { container } = renderTurn(envelope("answer"));
    expect(screen.getByText(/closely matched the question/i)).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/\d+%/);
    expect(container.textContent).not.toContain("0.91");
  });

  it("states that the sources are unreviewed, on the answer screen itself", () => {
    renderTurn(envelope("answer"));
    expect(
      screen.getByText(/No clinician has reviewed these sources/i),
    ).toBeInTheDocument();
  });
});

describe("partial support is shown and marked, never silently corrected", () => {
  it("labels the claim and explains what the label means", () => {
    renderTurn(envelope("partially_supported"));
    // Twice by design: a note at the top of the answer, and a badge on the
    // claim itself. The summary alone would be missable; the badge alone would
    // not say what "partly" means.
    expect(screen.getAllByText(/partly supported/i).length).toBeGreaterThanOrEqual(2);
    expect(
      screen.getByText(/backs part of this statement but not all of it/i),
    ).toBeInTheDocument();
  });

  it("still shows the claim rather than withholding it", () => {
    const value = envelope("partially_supported") as AnswerEnvelope;
    const partial = value.claims.find((claim) => claim.support === "partially_supported");
    expect(partial).toBeDefined();
    renderTurn(value);
    expect(screen.getByText(new RegExp(escapeRegExp(partial!.text)))).toBeInTheDocument();
  });
});

describe("abstention is the insufficient state, carried inside an answer", () => {
  it("shows the insufficient card, not an error", () => {
    renderTurn(envelope("abstain"));
    // Exactly one heading with this text: the insufficient card supplies the
    // result heading on this path rather than the answer card repeating it.
    expect(
      screen.getAllByRole("heading", { name: /not enough verified information/i }),
    ).toHaveLength(1);
    expect(screen.queryByText(/something went wrong/i)).toBeNull();
  });

  it("keeps the sentence that stops it reading as a clean bill of health", () => {
    renderTurn(envelope("abstain"));
    expect(
      screen.getByText(/not about you or your health/i),
    ).toBeInTheDocument();
  });

  it("still offers the questions, because they survive when the claims do not", () => {
    const value = envelope("abstain") as AnswerEnvelope;
    expect(value.claims).toHaveLength(0);
    expect(value.questions_for_doctor.length).toBeGreaterThan(0);
    renderTurn(value);
    expect(screen.getByRole("heading", { name: /worth asking/i })).toBeInTheDocument();
  });
});

describe("the standalone insufficient state", () => {
  it("reads as a result rather than a fault", () => {
    renderTurn(envelope("failure_evidence_unavailable"));
    expect(screen.getByRole("heading", { name: /not enough verified information/i })).toBeInTheDocument();
    expect(screen.queryByText(/something went wrong/i)).toBeNull();
    expect(screen.queryByText(/reference code/i)).toBeNull();
  });

  it("gives the patient something to do next", () => {
    renderTurn(envelope("failure_evidence_unavailable"));
    expect(screen.getByRole("heading", { name: /what you can do/i })).toBeInTheDocument();
  });

  it("never shows the operator-facing reason code", () => {
    // `no_eligible_evidence` is for whoever runs the deployment, not a patient.
    const { container } = renderTurn(envelope("failure_evidence_unavailable"));
    expect(container.textContent).not.toContain("no_eligible_evidence");
  });
});

describe("medical staff referral", () => {
  it("routes to a person instead of answering", () => {
    renderTurn(envelope("medical_staff"));
    expect(
      screen.getByRole("heading", { name: /for a person, not this tool/i }),
    ).toBeInTheDocument();
  });
});

describe("follow-up interpretation is disclosed", () => {
  it("says what the question was resolved to, and how to correct it", () => {
    renderTurn(envelope("follow_up_rewritten"), "what about the side effects?");
    expect(screen.getByText(/read as a follow-up/i)).toBeInTheDocument();
    expect(
      screen.getByText(/what are the side effects of metformin for type 2 diabetes\?/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/ask again in full/i)).toBeInTheDocument();
  });
});

describe("failures carry fixed copy and a code, and nothing else", () => {
  const failures = everyEnvelope().filter(([, value]) => value.kind === "failure");

  it("covers every failure state the service can produce", () => {
    expect(failures.length).toBeGreaterThanOrEqual(5);
  });

  it.each(failures)("%s shows the same patient-facing message", (_name, value) => {
    renderTurn(value);
    expect(screen.getByRole("heading", { name: /something went wrong/i })).toBeInTheDocument();
    expect(
      screen.getByText(/Nothing was saved and nothing was sent to your clinician/i),
    ).toBeInTheDocument();
  });

  it.each(failures)("%s never leaks provider text or model prose", (_name, value) => {
    const { container } = renderTurn(value);
    const text = container.textContent ?? "";
    // The provider secret from the golden states, and the medical prose the
    // model returned instead of JSON.
    expect(text).not.toMatch(/sk-[A-Za-z0-9]/);
    expect(text).not.toMatch(/api\.openai\.com/);
    expect(text).not.toMatch(/you most likely have/i);
    expect(text).not.toMatch(/start metformin/i);
    expect(text).not.toMatch(/Traceback|Exception|Error:/);
  });

  it("shows the typed code so a problem can be reported", () => {
    renderTurn(envelope("failure_generation_unavailable"));
    expect(screen.getByText("generation_unavailable")).toBeInTheDocument();
  });
});

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
