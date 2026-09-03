/**
 * Tailwind 4 is a PostCSS plugin now; there is no tailwind.config.js.
 * The design tokens live in `src/app/globals.css` under `@theme`.
 */
const config = {
  plugins: { "@tailwindcss/postcss": {} },
};

export default config;
