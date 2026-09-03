/**
 * Fail if the generated API types have drifted from the committed contract.
 *
 * `src/types/api.d.ts` is generated from `tests/snapshots/api/openapi.json`,
 * which the Python suite regenerates and pins. Committing the output and
 * checking it here means a backend contract change shows up as a failing
 * frontend gate rather than as a runtime surprise — and it means the types can
 * be read in review.
 */
import { execFileSync } from "node:child_process";
import { readFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const SPEC = "../tests/snapshots/api/openapi.json";
const COMMITTED = "src/types/api.d.ts";

const scratch = mkdtempSync(join(tmpdir(), "synapse-api-types-"));
const generated = join(scratch, "api.d.ts");

try {
  execFileSync("npx", ["openapi-typescript", SPEC, "-o", generated], { stdio: "pipe" });
  const fresh = readFileSync(generated, "utf8");
  const committed = readFileSync(COMMITTED, "utf8");
  if (fresh !== committed) {
    console.error(
      `${COMMITTED} is out of date with ${SPEC}.\n` +
        "The backend contract changed. Run `npm run gen:api` and read the diff.",
    );
    process.exit(1);
  }
  console.error(`OK: ${COMMITTED} matches ${SPEC}`);
} finally {
  rmSync(scratch, { recursive: true, force: true });
}
