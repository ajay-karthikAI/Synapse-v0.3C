import type { Metadata } from "next";

import { AccessForm } from "@/components/access/AccessForm";
import { Wordmark } from "@/components/brand/Wordmark";

export const metadata: Metadata = { title: "Access" };

/**
 * The passcode screen.
 *
 * The only page with no header at all — there is nowhere to go until a session
 * exists, so navigation would be furniture. The same centred composition as the
 * home page: mark, serif name, attribution, then the one thing to do.
 *
 * The mark animates here. It is the first thing anyone sees, and it is the one
 * place the trace is worth drawing.
 */
export default async function AccessPage({
  searchParams,
}: {
  searchParams: Promise<{ next?: string }>;
}) {
  const params = await searchParams;
  // Relative, single-slash paths only. An absolute or protocol-relative URL
  // here would be an open redirect: a caller could send a patient to
  // `/access?next=https://evil.example` and have this server bounce them there
  // after a successful sign-in. The server re-validates it too.
  const requested = params.next ?? "";
  const next =
    requested.startsWith("/") && !requested.startsWith("//") ? requested : undefined;

  return (
    <div className="mx-auto flex w-full max-w-md flex-col px-5 pb-20 pt-20 sm:pt-28">
      <Wordmark size="hero" />

      <p className="mt-8 text-center text-[17px] leading-relaxed text-ink-secondary">
        A private demonstration. Synapse is a pre-clinical prototype — not a
        medical device, and not clinically validated.
      </p>

      <div className="mt-10">
        <AccessForm next={next} />
      </div>

      <p className="mt-8 text-center text-[13px] leading-relaxed text-ink-secondary">
        Your session lasts eight hours and holds your conversation in memory
        only. Signing out discards it.
      </p>
    </div>
  );
}
