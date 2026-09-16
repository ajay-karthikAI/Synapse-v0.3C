import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SiteFooter } from "@/components/layout/SiteFooter";
import { SiteHeader } from "@/components/layout/SiteHeader";
import { SkipLink } from "@/components/layout/SkipLink";

/**
 * Navigation, sign-out, and the skip link.
 *
 * The properties worth pinning are the ones a redesign would quietly break:
 * the disclosure announces its state, sign-out is not reachable by navigation,
 * and the skip link exists and points somewhere real.
 */

const pathname = vi.hoisted(() => ({ current: "/" }));

vi.mock("next/navigation", () => ({
  usePathname: () => pathname.current,
}));

beforeEach(() => {
  pathname.current = "/";
});

describe("the skip link", () => {
  it("targets the main landmark", () => {
    render(<SkipLink />);
    const link = screen.getByRole("link", { name: /skip to main content/i });
    expect(link).toHaveAttribute("href", "#main");
  });

  it("is visually hidden until focused", () => {
    render(<SkipLink />);
    const link = screen.getByRole("link", { name: /skip to main content/i });
    expect(link).toHaveClass("visually-hidden");
    // ...and becomes visible on focus rather than staying hidden.
    expect(link.className).toMatch(/focus-visible:/);
  });
});

describe("the header when signed in", () => {
  it("shows the navigation", () => {
    pathname.current = "/transparency";
    render(<SiteHeader signedIn />);
    expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "Home" })[0]).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "About Synapse" })[0]).toBeInTheDocument();
  });

  it("marks the current page for assistive technology", () => {
    pathname.current = "/transparency";
    render(<SiteHeader signedIn />);
    const current = screen.getAllByRole("link", { name: "About Synapse" })[0];
    expect(current).toHaveAttribute("aria-current", "page");
    expect(screen.getAllByRole("link", { name: "Home" })[0]).not.toHaveAttribute("aria-current");
  });

  it("signs out with a POST form, never a link", () => {
    // A GET logout is reachable by prefetch, by a crawler, and by any page that
    // can cause a navigation — a CSRF that signs a patient out mid-question.
    render(<SiteHeader signedIn />);
    const button = screen.getAllByRole("button", { name: /sign out/i })[0];
    const form = button?.closest("form");
    expect(form).toHaveAttribute("method", "post");
    expect(form).toHaveAttribute("action", "/api/access/logout");
    expect(screen.queryByRole("link", { name: /sign out/i })).toBeNull();
  });
});

describe("the header when signed out", () => {
  it("offers no navigation and no sign-out", () => {
    render(<SiteHeader signedIn={false} />);
    expect(screen.queryByRole("navigation")).toBeNull();
    expect(screen.queryByRole("button", { name: /sign out/i })).toBeNull();
  });

  it("renders no header at all", () => {
    // The access screen is a single centred composition that owns the whole
    // viewport. A header above it would be chrome with nothing to navigate to.
    const { container } = render(<SiteHeader signedIn={false} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("responsive navigation", () => {
  it("collapses behind a disclosure that announces its state", async () => {
    const user = userEvent.setup();
    render(<SiteHeader signedIn />);

    const toggle = screen.getByRole("button", { name: "Menu" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveAttribute("aria-controls", "mobile-nav");
    // No panel exists while collapsed, so its links are not in the tab order —
    // the "eight invisible focus stops" defect from the previous interface.
    expect(document.getElementById("mobile-nav")).toBeNull();

    await user.click(toggle);

    const expanded = screen.getByRole("button", { name: "Close" });
    expect(expanded).toHaveAttribute("aria-expanded", "true");
    expect(document.getElementById("mobile-nav")).not.toBeNull();
  });

  it("closes when a destination is chosen", async () => {
    const user = userEvent.setup();
    render(<SiteHeader signedIn />);
    await user.click(screen.getByRole("button", { name: "Menu" }));

    const panel = document.getElementById("mobile-nav");
    const link = panel?.querySelector("a");
    expect(link).not.toBeNull();
    await user.click(link as HTMLAnchorElement);

    expect(screen.getByRole("button", { name: "Menu" })).toHaveAttribute("aria-expanded", "false");
  });

  it("gives every control a 44px minimum target", () => {
    render(<SiteHeader signedIn />);
    const toggle = screen.getByRole("button", { name: "Menu" });
    expect(toggle.className).toMatch(/min-h-\[44px\]/);
  });
});

describe("the footer", () => {
  it("states what this system is not, without being asked", () => {
    render(<SiteFooter />);
    const text = screen.getByText(/pre-clinical research prototype/i);
    expect(text).toHaveTextContent(/not a medical device/i);
    expect(text).toHaveTextContent(/not been clinically validated/i);
  });

  it("routes to the transparency page", () => {
    render(<SiteFooter />);
    expect(screen.getByRole("link", { name: "About Synapse" })).toHaveAttribute(
      "href",
      "/transparency",
    );
  });

  it("keeps the disclaimer focused on product limitations", () => {
    render(<SiteFooter />);
    const disclaimer = screen.getByText(/pre-clinical research prototype/i);
    expect(disclaimer.textContent).not.toMatch(/Company/);
  });
});
