import type { components } from "@/types/api";

/**
 * The wire contract, named.
 *
 * Every type here is an alias into `src/types/api.d.ts`, which is generated
 * from the committed OpenAPI document by `npm run gen:api`. Nothing in this
 * file describes a shape; it only gives the generated shapes usable names. That
 * matters more than it looks: a hand-written interface would compile happily
 * after the server changed, and the first sign of a mismatch would be a blank
 * region on a patient's screen. Here, a contract change becomes a type error.
 *
 * `TurnEnvelope` is a discriminated union on `kind`. Narrow on that tag before
 * reading any other field — `assertNever` at the end of a switch turns a state
 * nobody wrote a branch for into a compile error rather than a silent blank.
 */

type Schemas = components["schemas"];

export type AnswerEnvelope = Schemas["AnswerEnvelope"];
export type EmergencyEnvelope = Schemas["EmergencyEnvelope"];
export type InsufficientEnvelope = Schemas["InsufficientEnvelope"];
export type FailureEnvelope = Schemas["FailureEnvelope"];

export type TurnEnvelope =
  | AnswerEnvelope
  | EmergencyEnvelope
  | InsufficientEnvelope
  | FailureEnvelope;

export type Claim = Schemas["ClaimModel"];
export type Source = Schemas["SourceModel"];
export type Excerpt = Schemas["ExcerptModel"];
export type InsufficientDetail = Schemas["InsufficientModel"];

export type Brief = Schemas["BriefResponse"];
export type BriefQuestion = Schemas["BriefQuestionModel"];
export type BriefClaim = Schemas["BriefClaimModel"];

export type Transparency = Schemas["TransparencyResponse"];
export type SessionState = Schemas["SessionResponse"];

/** The three states an answer envelope can be in. */
export type AnswerAction = AnswerEnvelope["action"];

/**
 * Every envelope tag, as a value.
 *
 * `satisfies` ties this to the union: delete a member and the array stops
 * satisfying the type, so the runtime guard cannot fall behind the types.
 */
export const ENVELOPE_KINDS = [
  "answer",
  "emergency",
  "insufficient",
  "failure",
] as const satisfies readonly TurnEnvelope["kind"][];

/**
 * Whether a parsed JSON value is an envelope this client can render.
 *
 * The tag is checked, and nothing else. A deeper validation would duplicate the
 * server's pydantic models in TypeScript and drift from them; what the client
 * genuinely needs to know is which branch to take, and that a payload with no
 * recognised tag must not reach one. The renderers read only fields the tag
 * guarantees, and every one of them is rendered as text.
 */
export function isTurnEnvelope(value: unknown): value is TurnEnvelope {
  if (typeof value !== "object" || value === null) return false;
  const kind = (value as { kind?: unknown }).kind;
  return (
    typeof kind === "string" && (ENVELOPE_KINDS as readonly string[]).includes(kind)
  );
}

/**
 * The compile-time exhaustiveness check.
 *
 * Placed in the `default` of a switch over `kind`. If a fifth envelope is added
 * to the contract, every switch missing a branch for it fails to compile —
 * which is the whole reason the union is discriminated. It also throws, so a
 * server sending a tag this build has never heard of is a loud failure rather
 * than a blank space where an answer should be.
 */
export function assertNever(value: never): never {
  throw new Error(`Unhandled envelope variant: ${JSON.stringify(value)}`);
}

/**
 * Whether this turn can produce an appointment brief.
 *
 * Read from the server's own field rather than inferred from the tag. The two
 * agree today, but eligibility is the answer layer's decision — an abstention
 * is an answer envelope that still offers a brief, because the questions
 * survive even when the claims do not — and re-deriving it here would be a
 * second implementation of a rule that already has one.
 */
export function offersBrief(envelope: TurnEnvelope): boolean {
  return envelope.brief_available;
}
