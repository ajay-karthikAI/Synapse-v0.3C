import Link from "next/link";

import { BrandMark } from "@/components/brand/BrandMark";

/**
 * The mark, the name, and the attribution.
 *
 * Two arrangements, and the difference is the point:
 *
 * `hero` stacks them vertically and centres them — mark above name above
 * attribution. It is the first thing anyone sees and it is allowed to take up
 * room: the mark is set at 210px, as it was in the Streamlit interface, where
 * it was the whole of the identity. The name beneath it is in the display serif
 * at a size where the serif actually reads as one, which is what gives the
 * product a face rather than a logotype.
 *
 * `compact` sets them in a row for the header, at a size that does not compete
 * with the page beneath it. It takes the bold cut: Baskerville at 20px needs
 * the weight that the same face at 64px would be crushed by.
 *
 * The wordmark contains only the product name. Corporate attribution is not
 * part of the patient-facing identity.
 */

interface WordmarkProps {
  size?: "compact" | "hero";
  /** Wrap in a link home. Omitted on the access screen, where there is no home. */
  href?: "/" | undefined;
  animate?: boolean;
}

export function Wordmark({ size = "compact", href, animate = true }: WordmarkProps) {
  if (size === "hero") {
    return (
      <div className="flex flex-col items-center text-center">
        <BrandMark size="hero" animate={animate} />
        <h1 className="font-display mt-6 text-[3.25rem] leading-[1.05] font-normal text-display sm:text-[4rem]">
          {/*
            The name types itself in. The span is a purely visual wrapper: the
            text is in the DOM in full from the first paint, so a screen reader
            reads "Synapse" immediately regardless of where the animation is —
            the reveal is `clip-path`, which changes what is painted and not
            what is there.
          */}
          <span className={animate ? "brandmark-type" : undefined}>Synapse</span>
        </h1>
      </div>
    );
  }

  const content = (
    <span className="flex items-center gap-3">
      <BrandMark size="compact" animate={animate} />
      <span className="font-display text-[1.3rem] font-bold leading-none text-display">
        Synapse
      </span>
    </span>
  );

  if (href) {
    return (
      <Link
        href={href}
        className="rounded-[12px] no-underline"
        aria-label="Synapse — home"
      >
        {content}
      </Link>
    );
  }
  return content;
}
