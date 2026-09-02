# Privacy Threat Analysis and Logging Policy

**Applies to:** `synapse.telemetry` · `app.py` · every logging call in the repository
**Written:** 2026-08-20, **before** any telemetry code was implemented.
**Status:** policy in force, enforced by tests listed in §9.

> **This is not a compliance statement.** No privacy review, DPIA, or HIPAA
> assessment has been performed on Synapse, and **no claim of HIPAA compliance is
> made anywhere in this repository**. This document records what the system
> deliberately does and does not record, and why. See docs/PRIVACY_DATA_FLOW.md
> for where data goes at all, which is a separate and larger question.

---

## Part I — Threat analysis

### 1. Method and threat model

The analysis below asks four questions of every category of data the system
touches:

1. **What is it, concretely, and where does it exist today?**
2. **What becomes possible if it enters a telemetry record?**
3. **What is the decision, and what is the cost of that decision?**
4. **What enforces the decision** — a convention, a test, or a type?

The adversaries considered, in the order they matter for a waiting-room app:

- **A.** Someone with read access to operational logs or metrics: an on-call
  engineer, a contractor, a support tool, an SIEM, a log-shipping vendor. This
  is the realistic adversary. Logs are copied to more places than any other
  artifact, retained longer than intended, and read by more people than the
  system's designers imagine.
- **B.** Someone who obtains a telemetry export after the fact — a breach, a
  backup, a subpoena, an acquisition.
- **C.** Someone who can correlate telemetry with another dataset: a clinic
  appointment list, a building access log, a session recording.
- **D.** A future maintainer who adds a field without reading this document.
  Adversary D is the one most likely to actually occur, which is why §9 is
  mechanical rather than advisory.

A note on what makes this domain different: a health question is not merely
personal data, it is often a **diagnosis in progress**. "why do my hands shake
when I hold a cup" discloses a suspected condition before any clinician has
confirmed one. There is no anonymisation of a sentence like that which
preserves its analytic value, because its value *is* its content.

---

### 2. Category-by-category analysis

#### 2.1 Patient questions

**What it is.** Free text typed into the app. Users are not instructed to omit
identifying details and nothing stops them entering names, dates, employers,
medications or a full clinical history. Assume every query is sensitive personal
health information.

**Where it exists today.** Streamlit session memory for the session's lifetime;
sent to OpenAI for embedding, and again for generation. Never written to disk by
Synapse.

**If it entered telemetry.** Adversary A reads health conditions attached to a
timestamp. Adversary C joins timestamp against a clinic's appointment schedule
and attributes a question to a named person — in a waiting room, the set of
people present at 14:32 is small. This is the single highest-severity item in
the analysis.

**Decision: never recorded, in any form, by default.** Not the text, not a
prefix, not a summary, not a category inferred from it.

**Including hashes.** A digest of a query is *not* a safe derivative, and this
is worth stating precisely because it is a common mistake. Health questions are
drawn from a small, highly-repetitive space: an attacker with a candidate list
of a few hundred thousand phrasings ("chest pain", "chest pain left arm", …) can
hash the list once and recover the majority of queries by lookup. A per-
deployment secret salt raises the cost, but the salt lives in the same
environment the logs are shipped from, and a rotating salt destroys the
cross-run comparability that was the only reason to want the hash. **The
telemetry schema therefore carries no query digest at all** — a stricter rule
than the repository-wide logging guard, which permits `query_sha256`.

*Note on P1:* `StructuredAnswer.query_sha256` remains in the P1 answer schema and
is unchanged by this milestone. That field belongs to an answer record produced
and held under different rules; it is not a telemetry field and is not emitted by
`synapse.telemetry`.

**Cost accepted.** No per-topic analytics, no "what do patients ask about"
reporting, no query-level debugging. Volume, latency, outcome and failure code
are what remain, and §7 shows they are enough to run the system.

#### 2.2 Conversation history

**What it is.** The accumulated turns in `st.session_state.conversation`.

**If it entered telemetry.** Strictly worse than §2.1: a sequence of related
questions is far more identifying than any one of them, because the combination
narrows the population sharply. Three questions about a rare condition, a
medication and a child's age identify a family.

**Decision: never recorded.** Not the turns, not their contents, and no
turn-to-turn linkage beyond the short-lived session identifier in §5.

#### 2.3 Generated answers

**What it is.** The model's structured answer: summary, claims, doctor
evaluation, questions, limitations.

**If it entered telemetry.** An answer is a close paraphrase of the question that
produced it — "your HbA1c reflects average glucose" reveals the question was
about diabetes. Recording answers while omitting questions is a privacy control
that does not control anything.

**Decision: never recorded by default.** The schema has no field that can hold
answer text. Counts and the typed action are recorded instead.

**Deliberately not offered:** there is no "log answers in staging" flag. A flag
like that is turned on during an incident and turned off after the next one.

#### 2.4 Cited excerpts

**What it is.** Verbatim spans from PubMed abstracts, plus the chunk and
document identifiers they came from.

**Two distinct risks.** The excerpt *text* is third-party copyrighted content
and, in combination with an answer, reveals the clinical subject. The
*identifiers* are less obviously dangerous but not neutral: a chunk identifier
naming a rare-disease paper narrows the query as effectively as the text would.

**Decision:** excerpt text is never recorded. Identifiers are **counted, not
listed** — `evidence_count` and `candidate_count`, never `chunk_ids`. Retrieval
debugging that genuinely needs identifiers belongs in the offline evaluation
harness against authored queries, not in production telemetry.

#### 2.5 IP addresses

**What it is.** Present at the transport layer of any deployment.

**If used as an analytics identifier.** An IP is a household or a clinic. In a
waiting room it is *the clinic*, making every visitor's telemetry mutually
correlatable, and in many jurisdictions it is personal data in its own right.

**Decision: never collected, never derived from, never used as an identifier** —
including truncated or hashed forms, which remain re-identifying at clinic
granularity. Synapse's telemetry has no field for network address, and the
recorder is never passed a request object it could extract one from.

#### 2.6 Browser identifiers

**What it is.** Cookies, `localStorage`, canvas or font fingerprints, user-agent
strings.

**If used.** A stable identifier across sessions converts a series of anonymous
questions into a longitudinal health profile. This is the exact mechanism by
which analytics on health sites has caused real harm.

**Decision: no fingerprinting of any kind, and no persistent client
identifier.** The session identifier in §5 is server-side, random, and
short-lived by construction. The user-agent string is not recorded: it is
high-entropy enough to be a fingerprint on its own.

#### 2.7 API keys

**What it is.** The OpenAI key, from an environment variable, `.env`, or the
sidebar password field.

**Where it can leak into telemetry.** Rarely as a field — almost always inside
something else: an exception message quoting a request URL, a provider error
body, a configuration dump, a debug repr of a client object.

**Decision: never recorded, and actively removed.** Redaction runs over every
string that reaches a telemetry record, not only over fields believed to be
risky, because the leak always comes through a field nobody suspected. Patterns
and the property-based test that fuzzes them are in §9.

#### 2.8 Provider responses

**What it is.** The raw HTTP response from OpenAI: body, headers, request id,
rate-limit metadata.

**If recorded.** The body of a generation response *is* the answer (§2.3). Error
bodies quote the request, which quotes the prompt, which contains the question
and the retrieved passages. Headers carry organisation identifiers and, in some
error shapes, a key fragment.

**Decision:** record the **status class only** (`2xx`, `4xx`, `5xx`, `timeout`,
`network`), never the body, never headers, never the request id. The status
class is what distinguishes an operator's four possible responses; the body adds
nothing an operator can act on and everything a breach can use.

#### 2.9 Stack traces

**What it is.** Python tracebacks, including local variables in some formats.

**If recorded.** A traceback from the generation path has the prompt in a frame
local. A traceback from retrieval has passage text. Tracebacks are the single
most common accidental disclosure channel in any application that handles
sensitive input, because nobody thinks of them as data.

**Decision:** production telemetry records a **typed failure code** and the
exception **type name**, never a message, never a traceback, never frame
locals. The typed codes already exist (`synapse.ui.errors.AnswerFailureCode`)
and are a closed set this repository controls.

**Cost accepted, and the mitigation.** Debugging without traces is harder. The
runbook in docs/observability.md §6 is the compensating control: it shows how to
reach a root cause from codes, versions and stage timings, and how to reproduce
locally against authored queries where full traces are available.

#### 2.10 Feedback events

**What it is.** A future "was this helpful?" control.

**The trap.** Feedback is where free text re-enters a system that carefully
excluded it everywhere else — a comment box attached to a health question is a
health record, and it arrives with an implicit expectation of being read.

**Decision:** feedback is recorded as a **closed category only**
(`helpful`, `not_helpful`, `unclear`, `concerning`, `reported_error`). The schema
has no free-text feedback field. If free-text feedback is ever added it needs its
own consent flow, its own retention rule and its own review of this document — it
is out of scope here, and the schema is built so that adding one requires
changing this policy first.

#### 2.11 Exported appointment briefs

**What it is.** The plain-text brief a patient downloads
(`synapse.answer.brief`), containing the answer, the claims, the sources and the
verbatim excerpts.

**Where it exists.** Generated in memory, handed to the browser, never written to
server disk. It is the most sensitive single artifact the system produces:
question-adjacent content, clinical claims, and citations in one file.

**Decision:** the brief is **never recorded, never sampled, never counted by
content**. Telemetry may record that an export occurred (`action_result`), never
what it contained. The patient's copy is theirs; the server keeps nothing.

---

### 3. Residual risks, stated plainly

These are not solved by this milestone, and pretending otherwise would be the
real failure:

1. **The query still leaves the machine.** Telemetry hygiene does nothing about
   the three OpenAI calls per query (docs/PRIVACY_DATA_FLOW.md §2.1). That is
   the governing privacy fact about this system, and it is unchanged.
2. **Timing correlation survives.** A telemetry record with a timestamp, joined
   against a clinic's schedule, still narrows *who* asked *something*, even
   though it never says what. Coarser timestamps would reduce this; they would
   also destroy latency analysis, so the trade is not made here and is recorded
   as accepted.
3. **Volume patterns leak.** A spike in `action_result=emergency` at a small site
   discloses something about that site's patients that day.
4. **The deployment environment is trusted.** Nothing here protects against a
   compromised host, a malicious operator, or a log shipper configured to send
   records somewhere this document does not know about.
5. **No external analytics vendor has been reviewed**, because none is
   configured. Introducing one is a change to this document, not a
   configuration change.

---

## Part II — Policy

### 4. The rules

| # | Rule | Enforcement |
|---|---|---|
| 1 | Raw patient queries are never recorded, and never hashed into telemetry | Schema has no field; §9 tests |
| 2 | Raw answers and full excerpts are never recorded | Schema has no field; §9 tests |
| 3 | Secrets, keys, headers and authorization values are never recorded | Redaction on every string; fuzz test |
| 4 | No deterministic hash of a health query | Schema has no digest field; §2.1 |
| 5 | No IP address or browser fingerprint as an identifier | Never collected; no field exists |
| 6 | Telemetry fields are an explicit allowlist | `TelemetryEvent`, `extra="forbid"` |
| 7 | Unknown fields are rejected, not dropped silently | Validation error, then the event is dropped and counted |
| 8 | Exceptions are redacted before they reach a record | `redact_exception` |
| 9 | Typed error codes in production, never stack traces | `AnswerFailureCode` |
| 10 | Retention and deletion are configured and documented | §6 |
| 11 | Telemetry can be completely disabled | `TelemetryConfig(enabled=False)` → `NullSink` |
| 12 | Local development writes to a clearly-identified local-only sink | `LocalFileSink`, `environment="local"` |
| 13 | No HIPAA compliance claim | This document; docs/observability.md |

### 5. Identifiers

**`request_id`** — a fresh `uuid4` per request. Random, never derived from
content, never reused. Its only purpose is to join a metric to an event within
one deployment.

**`session_id`** — a random token that groups the turns of one sitting so that
"three retries in one session" is distinguishable from "three separate users".
It is:

- **random**, never derived from a user, a device, an address or a query;
- **short-lived** — it rotates after `session_ttl_seconds` (default 30 minutes)
  and on process restart, so it cannot become a longitudinal identifier;
- **held in memory only**, never set as a cookie, never returned to the browser;
- **not a user identifier**, and correlating two sessions to one person is not
  supported by anything in the system.

### 6. Retention and deletion

| Setting | Default | Meaning |
|---|---|---|
| `enabled` | `False` | Telemetry is **off** unless a deployment turns it on |
| `environment` | `local` | Recorded on every event; `local` marks a developer machine |
| `sink` | `null` | `null`, `memory` (tests) or `file` (local development) |
| `retention_days` | `30` | Records older than this are deleted by `prune()` |
| `session_ttl_seconds` | `1800` | Session identifier lifetime |
| `local_sink_path` | `artifacts/telemetry/` | Gitignored; local-only |

**Deletion.** `LocalFileSink.prune()` deletes records past `retention_days` and
is the documented deletion path; it is also what a deployment wires to a
scheduled job. Deletion is by file age because the records carry no subject
identifier — there is deliberately nothing to delete *by subject*, since there is
no subject recorded. That is a consequence of the design, not an omission: a
system that cannot answer "delete my data" because it never held any is in a
better position than one that can.

**No external destination is configured.** The file sink writes to local disk.
Shipping records anywhere is a deployment decision this repository does not make.

### 7. What remains answerable

The policy above removes the obvious analytics. What is left is sufficient to
operate the system, and stating that explicitly is what makes the trade
defensible:

- Is it up, and how fast — request counts, p50/p95 per stage, end to end.
- Is it failing, and how — outcome counts by typed code, timeout and error rates,
  retry counts, status classes.
- Is it behaving safely — abstention rate, emergency and medical-staff counts,
  degraded-rerank rate.
- What it costs — token counts and versioned cost estimates by model and prompt
  version.
- Did a release change any of the above — every event carries app, model, prompt,
  source-pack, corpus and index versions.

What is *not* answerable: what patients asked about. That is the point.

### 8. Changing this policy

Adding a telemetry field requires: adding it to `TelemetryEvent`, adding a row to
§2 of this document explaining the disclosure risk, and a test. The schema
rejects unknown fields, so a field added without the first step fails at runtime;
the review is what catches the second.

### 9. Enforcement

| Rule | Test |
|---|---|
| No query text or digest | `tests/test_telemetry_privacy.py::TestNoPatientTextEverReachesTelemetry` |
| No answer or excerpt text | same |
| Secret redaction | `TestSecretRedaction`, plus `TestRedactionFuzz` (property-based, 10k generated cases) |
| Unknown fields rejected | `TestSchemaIsAnAllowlist` |
| Disable works | `TestTelemetryCanBeDisabled` |
| Sink failure is contained | `TestSinkFailureNeverBreaksAnswering` |
| No free-text log fields anywhere | `tests/test_no_pickle_and_imports.py` (pre-existing AST sweep) |
