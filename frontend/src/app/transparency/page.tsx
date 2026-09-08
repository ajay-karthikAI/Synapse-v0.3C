import type { Metadata } from "next";

import type { Transparency } from "@/lib/envelope";
import { callBackend } from "@/lib/backend";
import { isConfigured } from "@/lib/env";

export const metadata: Metadata = { title: "About Synapse" };
// The payload reports what this deployment is running right now, including its
// readiness and its last evaluation. A cached copy would eventually describe a
// deployment that no longer exists.
export const dynamic = "force-dynamic";

/**
 * The transparency page.
 *
 * Editorial: a narrow measure, serif headings, generous leading, no cards. It
 * is meant to be read rather than scanned, because what it says is the least
 * convenient thing about the product and putting it in tiles would be a way of
 * not quite saying it.
 *
 * **The disclosures fail closed.** `FALLBACK_DISCLOSURES` mirrors the server's
 * `MANDATORY_DISCLOSURES` and is rendered whenever the live payload cannot be
 * fetched. A transparency page that goes blank when the backend is down is
 * worse than no page at all: the reader is left believing there was nothing to
 * disclose. So the unreviewed-source status, the not-a-device statement and the
 * evaluation caveat are present on every render, from this file, whatever the
 * network did.
 *
 * **Denominators travel with numbers.** An evaluation metric is never printed
 * as a bare percentage. "100%" over one case and "100%" over two hundred are
 * different claims, and only one of them is worth reading.
 *
 * **Operator metrics live here, not in the chat.** Readiness, index identity,
 * model versions and evaluation counts are on this page because someone
 * assessing the system needs them. They are absent from the answer surface,
 * where they would read as a quality score attached to a patient's own result.
 */

/** Rendered when the live payload is unavailable. Mirrors MANDATORY_DISCLOSURES. */
const FALLBACK_DISCLOSURES = [
  "Synapse is a pre-clinical research prototype. It is not a medical device, is not FDA cleared, and is not HIPAA compliant.",
  "No clinician has reviewed any source, evaluation case, threshold, or output in this deployment.",
  "The sources currently served are UNREVIEWED. They must not be described as approved or clinically validated.",
  "Every evaluation case is synthetic and unreviewed, and none is eligible to gate a release. Reported metrics measure whether the system does what its author expected, not whether that expectation is clinically correct.",
  "Answers are drawn from published abstracts, which routinely omit study population, dosing, harms and contraindications.",
  "Synapse does not diagnose, triage, or advise on medication. If something feels urgent, contact your clinic or your local emergency number.",
];

async function loadTransparency(): Promise<Transparency | null> {
  if (!isConfigured()) return null;
  try {
    const response = await callBackend({ path: "/v1/transparency", method: "GET" });
    if (!response.ok) return null;
    return (await response.json()) as Transparency;
  } catch {
    // Unreachable backend. The page still renders; see the note above.
    return null;
  }
}

export default async function TransparencyPage() {
  const data = await loadTransparency();
  const disclosures = data?.disclosures ?? FALLBACK_DISCLOSURES;

  return (
    <article className="mx-auto w-full max-w-2xl px-5 pb-24 pt-14 sm:pt-20">
      <h1 className="font-display text-[2.5rem] leading-[1.1] font-semibold text-display sm:text-[3rem]">
        About Synapse
      </h1>
      <p className="mt-5 text-lg leading-relaxed text-ink-secondary">
        Synapse is a medical AI chatbot that searches published research to
        answer the patient&rsquo;s questions. It also suggests questions to
        raise with their physician.
      </p>

      {data === null ? (
        <p
          role="status"
          className="mt-8 rounded-[12px] border border-rule bg-surface px-4 py-3 text-[13px] leading-relaxed text-ink-secondary"
        >
          Live deployment details could not be read just now. The statements
          below are fixed in this application and apply regardless.
        </p>
      ) : null}

      {/* --- What it is for, and what it is not for --- */}
      <Section id="intended-use" title="What it is for">
        <p className="text-[16px] leading-relaxed text-ink">
          Synapse is for preparing to talk to a clinician. You ask about
          something you want to understand, and it searches published research
          and returns what it found — with the exact passage each statement came
          from, and a list of questions worth raising at your appointment.
        </p>
        <p className="mt-4 text-[16px] leading-relaxed text-ink">
          It is intended for adults preparing for a routine, non-urgent
          appointment, using questions of the kind a patient might otherwise
          search for. It has been built and tested by one engineer against
          synthetic cases.
        </p>
      </Section>

      <Section id="exclusions" title="What it is not for">
        <ul className="space-y-3 text-[16px] leading-relaxed text-ink">
          <li>
            <strong className="font-semibold">Not for emergencies.</strong> If
            something feels urgent, contact your clinic or your local emergency
            number. Synapse cannot judge urgency and does not try to.
          </li>
          <li>
            <strong className="font-semibold">Not for diagnosis.</strong> It
            will not tell you what you have, and it is not able to.
          </li>
          <li>
            <strong className="font-semibold">Not for medication decisions.</strong>{" "}
            It will not advise starting, stopping or changing anything you take.
          </li>
          <li>
            <strong className="font-semibold">Not for children, pregnancy, or
            complex conditions.</strong> Nothing in it has been evaluated for
            those, and abstracts routinely omit the population a finding applies
            to.
          </li>
          <li>
            <strong className="font-semibold">Not a clinical record.</strong>{" "}
            Nothing you type is stored, and nothing reaches your clinician
            unless you carry it there yourself.
          </li>
        </ul>
      </Section>

      {/* --- The disclosures, from the server where possible --- */}
      <Section id="limits" title="What this system is not">
        <ul className="space-y-4">
          {disclosures.map((statement) => (
            <li key={statement} className="text-[16px] leading-relaxed text-ink">
              {statement}
            </li>
          ))}
        </ul>
      </Section>

      {/* --- Sources --- */}
      {data ? <SourcesSection sources={data.sources} /> : null}

      {/* --- Evaluation --- */}
      {data ? <EvaluationSection evaluation={data.evaluation} /> : null}

      {/* --- Safety boundaries --- */}
      <Section id="safety" title="Where the system stops itself">
        <ul className="space-y-3 text-[16px] leading-relaxed text-ink">
          <li>
            Your words are checked for emergency signs{" "}
            <strong className="font-semibold">before</strong> anything is
            searched or generated. If one is found, the search never runs and no
            research is shown.
          </li>
          <li>
            Every statement is checked against a retrieved passage before you see
            it. Statements that fail are removed, not softened — which is why
            Synapse sometimes shows you nothing and says so.
          </li>
          <li>
            Nothing is streamed to you word by word. You see a complete result
            that has already been checked, because showing unverified text and
            withdrawing it later is its own harm.
          </li>
          <li>
            When something breaks, you get a fixed message and a reference code.
            The underlying error text never reaches the page — it can contain
            things that should not be shown.
          </li>
          <li>
            Once an emergency has been flagged in a conversation, later questions
            in that same conversation stay flagged. A follow-up like &ldquo;is
            that serious?&rdquo; carries no warning signs of its own.
          </li>
        </ul>
      </Section>

      {/* --- What the deployment does not protect against --- */}
      <Section id="privacy" title="What this does not protect you from">
        <ul className="space-y-2.5 space-y-2.5 text-[15px] leading-relaxed text-ink-secondary">
          <li>
            This is not a HIPAA-compliant service and has no agreement covering
            personal health information. Do not enter anything that identifies
            you.
          </li>
          <li>
            Text sent to a model provider is outside this system&rsquo;s control
            once it leaves. Retention there is governed by that provider.
          </li>
          <li>
            Anyone with the shared passcode can use this deployment. It
            identifies a group, not a person, and it is not a personal account.
          </li>
          <li>
            A downloaded brief is an ordinary file on your device. Nothing here
            protects it after that.
          </li>
        </ul>
      </Section>

    </article>
  );
}

function Section({
  id,
  title,
  children,
}: {
  id: string;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section aria-labelledby={id} className="mt-14">
      <h2
        id={id}
        // Display serif, as on the question screen. Sized between the body
        // (16px) and the page title (40-48px), and no longer uppercase: wide
        // tracking on small caps is an eyebrow label, which is not what these
        // are now that they carry the serif and the display ink.
        className="font-display text-[1.375rem] leading-snug font-semibold text-display sm:text-[1.5rem]"
      >
        {title}
      </h2>
      <div className="mt-5">{children}</div>
    </section>
  );
}

/** `sources` and friends are typed `object` on the wire; read them defensively. */
function field(record: object, key: string): unknown {
  return (record as Record<string, unknown>)[key];
}

function text(value: unknown): string {
  return typeof value === "string" || typeof value === "number" ? String(value) : "—";
}

function SourcesSection({ sources }: { sources: object }) {
  const available = field(sources, "available") === true;
  return (
    <Section id="sources" title="The sources it searches">
      {available ? (
        <dl className="space-y-3">
          <Row label="Source pack" value={text(field(sources, "title"))} />
          <Row
            label="Version"
            value={`${text(field(sources, "pack_id"))} · ${text(field(sources, "pack_version"))}`}
          />
          <Row label="Sources in the pack" value={text(field(sources, "total_sources"))} />
          <Row
            label="Eligible to be served"
            value={text(field(sources, "approved_count"))}
          />
          <Row label="Reviewed by a clinician" value="No — none of them" />
        </dl>
      ) : (
        <p className="text-[16px] leading-relaxed text-ink">
          No source pack could be read, so no source is authorised for serving.
        </p>
      )}
      <p className="mt-5 text-[14px] leading-relaxed text-ink-secondary">
        &ldquo;Eligible&rdquo; means a source passed this system&rsquo;s own
        lifecycle rules. It does not mean a person with medical training looked
        at it. Nobody has.
      </p>
    </Section>
  );
}

function EvaluationSection({ evaluation }: { evaluation: object }) {
  const available = field(evaluation, "available") === true;
  const metrics = field(evaluation, "headline_metrics");
  const rows =
    available && typeof metrics === "object" && metrics !== null
      ? Object.entries(metrics as Record<string, unknown>)
      : [];

  return (
    <Section id="evaluation" title="What has been measured">
      {available ? (
        <>
          <dl className="space-y-3">
            <Row label="Run" value={text(field(evaluation, "run_id"))} />
            <Row label="Dataset" value={text(field(evaluation, "dataset_version"))} />
            <Row label="Cases" value={text(field(evaluation, "case_count"))} />
          </dl>
          {rows.length > 0 ? (
            <ul className="mt-5 space-y-2.5">
              {rows.map(([name, entry]) => {
                const record = typeof entry === "object" && entry !== null ? entry : {};
                const denominator = field(record, "denominator");
                return (
                  <li key={name} className="text-[15px] leading-relaxed text-ink">
                    <span className="text-ink-secondary">{name}:</span>{" "}
                    {text(field(record, "value"))}
                    {/* The denominator is not optional. A rate without one is
                        not a measurement, it is a shape. */}
                    <span className="text-ink-secondary">
                      {" "}
                      over {text(denominator)}{" "}
                      {denominator === 1 ? "case" : "cases"}
                    </span>
                  </li>
                );
              })}
            </ul>
          ) : null}
        </>
      ) : (
        <p className="text-[16px] leading-relaxed text-ink">
          No evaluation run is present in this deployment. Metrics are produced
          offline against a fixed dataset, never from live questions — a real
          question has no correct answer recorded to score against.
        </p>
      )}
      <p className="mt-5 text-[14px] leading-relaxed text-ink-secondary">
        Every case is synthetic and was written by the same person who wrote the
        code it tests. No result here is eligible to gate a release, and none of
        it is evidence that the system is clinically correct.
      </p>
    </Section>
  );
}


function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-wrap justify-between gap-x-6 gap-y-1 border-b border-rule pb-2.5">
      <dt className="text-[14px] text-ink-secondary">{label}</dt>
      <dd className="text-[14px] text-ink">{value}</dd>
    </div>
  );
}

