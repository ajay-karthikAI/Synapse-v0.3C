"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";

import { Wordmark } from "@/components/brand/Wordmark";

/**
 * The header, kept deliberately quiet.
 *
 * On the home page the composition below it is the design, so the header
 * carries no wordmark at all — repeating the name six inches above a 64px serif
 * setting of the same name is noise. Everywhere else it appears compactly, as
 * the way back.
 *
 * Navigation is plain text links, not buttons: they navigate, and dressing a
 * link as a button makes it less obvious what it does, not more.
 *
 * Sign-out is a form POST rather than a link. A link would be reachable by
 * prefetch, by a crawler, and by any page that can cause a navigation — a
 * cross-site request forgery that signs a patient out mid-question.
 */

interface NavItem {
  href: "/" | "/transparency";
  label: string;
}

const NAV: NavItem[] = [
  { href: "/", label: "Home" },
  { href: "/transparency", label: "About Synapse" },
];

interface SiteHeaderProps {
  signedIn: boolean;
}

export function SiteHeader({ signedIn }: SiteHeaderProps) {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  const onHome = pathname === "/";

  if (!signedIn) {
    // The access screen owns the whole viewport. Nothing above it.
    return null;
  }

  return (
    <header className="sticky top-0 z-40 border-b border-rule/70 bg-canvas/85 backdrop-blur-md">
      <div className="mx-auto flex h-14 max-w-5xl items-center justify-between gap-4 px-5">
        {/* Absent on home: the hero states the name at four times this size. */}
        <div className="min-w-0">{onHome ? null : <Wordmark href="/" animate={false} />}</div>

        <nav aria-label="Main" className="hidden items-center gap-6 md:flex">
          {NAV.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              aria-current={pathname === item.href ? "page" : undefined}
              className={`text-[14px] no-underline transition-colors ${
                pathname === item.href
                  ? "font-medium text-display"
                  : "text-ink-secondary hover:text-display"
              }`}
            >
              {item.label}
            </Link>
          ))}
          <SignOutButton />
        </nav>

        <button
          type="button"
          aria-expanded={open}
          aria-controls="mobile-nav"
          onClick={() => setOpen((value) => !value)}
          className="inline-flex min-h-[44px] min-w-[44px] items-center justify-center rounded-[12px] border border-rule px-3 text-sm font-medium text-display md:hidden"
        >
          {open ? "Close" : "Menu"}
        </button>
      </div>

      {open ? (
        <nav
          id="mobile-nav"
          aria-label="Main"
          className="border-t border-rule bg-surface px-5 pb-4 pt-2 md:hidden"
        >
          <ul className="flex flex-col">
            {NAV.map((item) => (
              <li key={item.href}>
                <Link
                  href={item.href}
                  aria-current={pathname === item.href ? "page" : undefined}
                  onClick={() => setOpen(false)}
                  className={`block min-h-[44px] py-3 text-[15px] no-underline ${
                    pathname === item.href
                      ? "font-medium text-display"
                      : "text-ink-secondary"
                  }`}
                >
                  {item.label}
                </Link>
              </li>
            ))}
            <li className="pt-2">
              <SignOutButton />
            </li>
          </ul>
        </nav>
      ) : null}
    </header>
  );
}

function SignOutButton() {
  return (
    <form action="/api/access/logout" method="post">
      <button
        type="submit"
        className="min-h-[44px] text-[14px] text-ink-secondary transition-colors hover:text-display"
      >
        Sign Out
      </button>
    </form>
  );
}
