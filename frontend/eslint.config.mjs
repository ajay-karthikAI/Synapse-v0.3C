import nextCoreWebVitals from "eslint-config-next/core-web-vitals";
import nextTypescript from "eslint-config-next/typescript";

/**
 * Flat config, imported directly.
 *
 * `eslint-config-next@16` ships flat configs as real exports, so the
 * `FlatCompat` shim is both unnecessary and broken here — it tries to
 * JSON-stringify a plugin object that references itself.
 *
 * ESLint is pinned to 9.x, not 10.x, and that is deliberate. The
 * `eslint-plugin-react` bundled inside `eslint-config-next@16.3.4` calls a
 * rule-context API that ESLint 10 removed (`contextOrFilename.getFilename`),
 * so linting crashes outright on 10. The pin follows the brief's criterion —
 * versions compatible with Next.js 16 — and moves when the config's own
 * dependency tree does. Same reason TypeScript is pinned to 5.9 rather than the
 * 7.x native port: `eslint-config-next` depends on `typescript-eslint@^8`,
 * which supports TypeScript up to 5.9.
 */
const config = [
  ...nextCoreWebVitals,
  ...nextTypescript,
  {
    ignores: [
      ".next/**",
      "node_modules/**",
      "coverage/**",
      "playwright-report/**",
      "test-results/**",
      // Generated from the backend's OpenAPI contract; reviewed as a diff, not
      // as hand-written code.
      "src/types/api.d.ts",
    ],
  },
  {
    rules: {
      /*
       * The project-wide invariant, enforced rather than reviewed: dynamic data
       * is rendered as text, never as markup. React makes that the default;
       * this makes the exception an error.
       */
      "react/no-danger": "error",
      "react/no-danger-with-children": "error",
      "@typescript-eslint/no-explicit-any": "error",
      "@typescript-eslint/consistent-type-imports": "error",
      // `console.log` in a request path is how a query reaches a log aggregator.
      "no-console": ["error", { allow: ["error", "warn"] }],
    },
  },
];

export default config;
