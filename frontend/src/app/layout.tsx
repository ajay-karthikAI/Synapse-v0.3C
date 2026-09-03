import type { Metadata, Viewport } from "next";
import { GeistSans } from "geist/font/sans";
import { Libre_Baskerville } from "next/font/google";
import { cookies, headers } from "next/headers";

import { SiteFooter } from "@/components/layout/SiteFooter";
import { SiteHeader } from "@/components/layout/SiteHeader";
import { SkipLink } from "@/components/layout/SkipLink";
import { ACCESS_COOKIE, verifyAccessToken } from "@/lib/session";

import "./globals.css";

/**
 * Libre Baskerville for the wordmark and headings.
 *
 * A Baskerville cut for the screen: a large x-height and generous letterforms,
 * which is why it holds at small sizes where a print revival would close up.
 *
 * It ships only 400 and 700 — there is no 600 — so the wordmark asks for a
 * weight that actually exists rather than letting the browser synthesise one.
 * Self-hosted by `next/font`, so no request leaves for a font CDN, which is
 * also why the CSP needs no third-party `font-src`.
 */
const libreBaskerville = Libre_Baskerville({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-libre-baskerville",
  weight: ["400", "700"],
});

export const metadata: Metadata = {
  title: { default: "Synapse", template: "%s · Synapse" },
  description:
    "Prepare for a medical appointment with questions drawn from published research. A pre-clinical research prototype; not a medical device.",
  // No search engine should index a demo that answers medical questions.
  robots: { index: false, follow: false, nocache: true },
  applicationName: "Synapse",
  formatDetection: { telephone: false, email: false, address: false },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  // Never block zoom. Pinch-zoom is the assistive technology most people
  // actually use, and disabling it fails WCAG 1.4.4.
  maximumScale: 5,
  themeColor: "#faf9fc",
};

export default async function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  // Read once per request, in the layout, so the header does not need a client
  // round-trip to know whether to show navigation.
  const cookieStore = await cookies();
  const session = await verifyAccessToken(
    cookieStore.get(ACCESS_COOKIE)?.value,
    process.env.SYNAPSE_JWT_SECRET ?? "",
  );

  // The nonce middleware minted for this response. Next puts it on its own
  // scripts, which is what lets the CSP omit 'unsafe-inline'.
  const nonce = (await headers()).get("x-nonce") ?? undefined;

  return (
    <html lang="en" className={`${GeistSans.variable} ${libreBaskerville.variable}`}>
      <body className="flex min-h-dvh flex-col antialiased" {...(nonce ? { nonce } : {})}>
        <SkipLink />
        <SiteHeader signedIn={session !== null} />
        <main id="main" tabIndex={-1} className="flex-1 focus-visible:outline-none">
          {children}
        </main>
        <SiteFooter />
      </body>
    </html>
  );
}
