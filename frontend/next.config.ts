import type { NextConfig } from "next";

/**
 * Security headers that do not vary per request.
 *
 * The Content-Security-Policy is NOT here — it carries a per-request nonce and
 * is therefore set in `src/middleware.ts`. Everything below is constant, so it
 * belongs where it applies to every response including static assets, which
 * middleware does not always see.
 */
const securityHeaders = [
  // This site is never framed. Clickjacking a passcode field is the specific
  // thing being prevented. `frame-ancestors 'none'` in the CSP says the same
  // to modern browsers; this covers the rest.
  { key: "X-Frame-Options", value: "DENY" },
  // No MIME sniffing. A brief export served as text must not be re-interpreted.
  { key: "X-Content-Type-Options", value: "nosniff" },
  // Never leak a URL to a third party. Paths here are not sensitive today, but
  // the default (`strict-origin-when-cross-origin`) still sends the origin.
  { key: "Referrer-Policy", value: "no-referrer" },
  // Deny every powerful feature. This application needs none of them, and a
  // deny-list written once is cheaper than auditing what a dependency asks for.
  {
    key: "Permissions-Policy",
    value: [
      "accelerometer=()",
      "autoplay=()",
      "camera=()",
      "display-capture=()",
      "encrypted-media=()",
      "fullscreen=()",
      "geolocation=()",
      "gyroscope=()",
      "magnetometer=()",
      "microphone=()",
      "midi=()",
      "payment=()",
      "publickey-credentials-get=()",
      "screen-wake-lock=()",
      "usb=()",
      "xr-spatial-tracking=()",
      "browsing-topics=()",
      "interest-cohort=()",
    ].join(", "),
  },
  // Cross-origin isolation. Nothing here is embedded anywhere.
  { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
  { key: "Cross-Origin-Resource-Policy", value: "same-origin" },
  { key: "X-DNS-Prefetch-Control", value: "off" },
];

// HSTS is NOT here. It moved to `src/middleware.ts`, which can see the request
// host and therefore knows whether the response is actually travelling over
// HTTPS — a question this file cannot ask, and which `NODE_ENV` answers wrongly
// (`next start` sets it to "production" and is how this runs locally).
const productionHeaders: { key: string; value: string }[] = [];

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Next writes AGENTS.md and CLAUDE.md into the project on first dev run.
  // This repository does not want generated agent files appearing in a diff.
  agentRules: false,
  // Do not advertise the framework version to a scanner.
  poweredByHeader: false,
  // A trailing-slash redirect is one more place a URL can be rewritten.
  trailingSlash: false,
  typedRoutes: true,

  async headers() {
    return [
      {
        source: "/:path*",
        headers: [...securityHeaders, ...productionHeaders],
      },
    ];
  },
};

export default nextConfig;
