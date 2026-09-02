# Security Policy

## Reporting a vulnerability

**Do not open a public issue for a security vulnerability.**

Use GitHub's private reporting: **Security → Advisories → Report a
vulnerability** on this repository. If that is unavailable, contact the
repository owner directly through their GitHub profile.

Please include: what you found, how to reproduce it, what an attacker gains,
and any suggested fix. A proof of concept helps.

**Expected response:** this is a research prototype maintained by one person.
There is no funded security team and no guaranteed response time. Reports will
be acknowledged as soon as practical. Do not assume a fix is imminent, and do
not deploy this system anywhere a delayed fix would matter.

## Scope

**In scope**

- The `synapse` package: schema validation, artifact loading, corpus and
  manifest parsing, governance state transitions, the evaluation harness, the
  answer and citation layer.
- CI workflow configuration under `.github/workflows/`.
- Anything that causes untrusted input to be executed, or unsupported medical
  claims to be displayed to a user.

**Out of scope**

- The legacy prototype (`app.py`, `Data/`, `Retrieval/`, `Generation/`). It is
  known to be unhardened, is excluded from lint and type-check, and is being
  migrated. Findings there are welcome but are already assumed.
- Vulnerabilities in PubMed, OpenAI, or other third-party services.
- Missing hardening that the documentation already discloses — see
  [docs/LIMITATIONS.md](docs/LIMITATIONS.md) before reporting.

## Security properties this repository tries to hold

These are the invariants worth attacking. Each has a test.

| Property | Enforced by |
|---|---|
| No runtime path deserialises pickle | AST-based test over every module; the one legacy reader is quarantined in `synapse/_legacy/` behind a restricted unpickler with an exact `(module, name)` allow-list |
| Corpus files and manifests are untrusted input | Every load goes through a pydantic model with `extra="forbid"`; artifact hashes are verified before use |
| No `eval`, `exec`, dynamic imports, or unsafe YAML | Ruff `S` (bandit) rules in CI; configuration is TOML parsed by stdlib `tomllib` |
| Validation failures are never swallowed | No blanket `except Exception` in `synapse/`; the single deliberate one records the error class and never discards it |
| Patient query text stays out of logs | Logging layer omits query text by default; see [docs/PRIVACY_DATA_FLOW.md](docs/PRIVACY_DATA_FLOW.md) |
| CI never sees a secret | `ci.yml` declares `permissions: contents: read` and references no secrets; a step fails the build if an API key is present in the environment |
| Actions cannot be repointed at malicious code | Every GitHub Action is pinned by commit SHA, not by tag |
| Unsupported claims are never displayed | Citation verification with lexical support checking; unsupported claims are withheld, and adversarial tests assert it |

## Known unhardened areas

Disclosed rather than discovered:

- **The legacy app renders LLM output.** Escaping was added, but the prototype
  has not had a dedicated injection review.
- **Prompt injection from retrieved content is only partially mitigated.** The
  evaluation set contains adversarial injection cases; they are synthetic, and
  passing them is not proof of resistance.
- **No authentication, authorisation, rate limiting, or audit logging.** The
  app has no concept of a user account.
- **No secret is rotated or scoped.** `OPENAI_API_KEY` is read from the
  environment and used directly.

## Handling of credentials

No credential should ever be committed. `.env` is gitignored, CI asserts that no
`.env`, `.pem`, or key-shaped file is tracked, and a secret scanner runs over
history. If you believe a key was committed, treat it as compromised and rotate
it immediately — removing it from a later commit does not remove it from
history.
