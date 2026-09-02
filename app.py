"""
Synapse — A Zenith Company
Dark, mobile-first waiting room UI
"""

import os
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path

import streamlit as st

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).parent))

# Configure the synapse logger hierarchy once, at the entry point. Library
# modules never configure logging themselves, so without this the structured
# fields attached to every warning — including the error type and HTTP status
# behind a failed generation — were discarded, leaving an operator with only
# the reference code shown on the failure card.
from synapse.logging import configure_logging
configure_logging(os.getenv("SYNAPSE_LOG_LEVEL", "INFO"))

# Telemetry is DISABLED unless SYNAPSE_TELEMETRY=1. When enabled without a named
# sink it writes to local disk under artifacts/telemetry/. It records durations,
# versions, counts, outcomes and typed failure codes — never the question, the
# answer, or any excerpt. See docs/privacy-logging-policy.md.
from synapse.telemetry import TelemetryRecorder
TELEMETRY = TelemetryRecorder.from_environment(app_version="0.1.0")

# Governed source metadata for the evidence cards. Loaded once; every field an
# evidence card shows is copied from this pack, and a source the pack does not
# describe renders as "no governance record" rather than with invented fields.
@st.cache_resource(show_spinner=False)
def _load_metadata_resolver():
    """Load the source pack, or an empty resolver if none is configured."""
    from pathlib import Path as _P

    from synapse.evidence import MetadataResolver

    pack_dir = _P(os.getenv("SYNAPSE_SOURCE_PACK", "source_packs/diabetes-previsit"))
    try:
        return MetadataResolver.from_directory(pack_dir)
    except Exception:  # A missing or malformed pack must not stop the app serving
        return MetadataResolver.empty()

RESOLVER = _load_metadata_resolver()

st.set_page_config(
    page_title="Synapse",
    page_icon="🫀",
    layout="centered",
    initial_sidebar_state="collapsed",
)

from synapse.a11y.palette import css_variables

st.markdown(f"""
<style>
{css_variables()}
</style>
""", unsafe_allow_html=True)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter+Tight:ital,wght@0,300;0,400;0,500;0,600;0,700;0,800;0,900;1,300;1,400&display=swap');

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

html, body, [class*="css"] {
  font-family: 'Inter Tight', sans-serif;
  background-color: #05030D;
  color: #e8e8e8;
}

.stApp {
  background:
    radial-gradient(circle at 50% 30%, rgba(138,92,246,0.25), transparent 40%),
    linear-gradient(180deg, #0E0A1F 0%, #05030D 100%) !important;
  min-height: 100vh;
}

/* Edge streaks — on body so they escape all Streamlit containers */
body::before, body::after {
  content: '';
  position: fixed;
  bottom: -30px;
  width: 4px;
  height: 320px;
  background: linear-gradient(to top, rgba(167,139,250,1) 0%, rgba(139,92,246,0.75) 35%, rgba(109,40,217,0.35) 65%, transparent 100%);
  box-shadow: 0 0 22px 7px rgba(139,92,246,0.75), 0 0 65px 18px rgba(109,40,217,0.4);
  border-radius: 3px;
  pointer-events: none;
  z-index: 99999;
  animation: streak-breathe 3.5s ease-in-out infinite;
}
body::before { left: 38px;  transform: rotate(-20deg); transform-origin: bottom center; }
body::after  { right: 38px; transform: rotate(20deg);  transform-origin: bottom center; animation-delay: 0.7s; }

/* Requirement 24: the app used to hide #MainMenu, header and the toolbar with
   display:none, which removes them from the accessibility tree entirely. That
   menu is where Streamlit keeps Settings (theme, wide mode) and Print, and the
   header is where the keyboard focus sequence begins. They are restored.

   Only the "Made with Streamlit" footer stays hidden: it is promotional, it is
   not a control, and hiding it removes nothing a user needs. */
footer { visibility: hidden; display: none; }

.block-container {
  max-width: 560px;
  padding: 0 1.25rem 7rem 1.25rem;
  margin: 0 auto;
}

/* Animated edge streaks */
.streak-left, .streak-right {
  position: fixed;
  bottom: -24px;
  width: 2px;
  height: 270px;
  background: linear-gradient(to top, rgba(139,92,246,1) 0%, rgba(109,40,217,0.55) 55%, transparent 100%);
  box-shadow: 0 0 18px 3px rgba(139,92,246,0.65), 0 0 55px 8px rgba(109,40,217,0.35);
  border-radius: 2px;
  pointer-events: none;
  z-index: 9999;
  animation: streak-breathe 3.5s ease-in-out infinite;
}
.streak-left  { left: 52px;  transform: rotate(-22deg); transform-origin: bottom center; }
.streak-right { right: 52px; transform: rotate(22deg);  transform-origin: bottom center; animation-delay: 0.6s; }

@keyframes streak-breathe {
  0%, 100% { opacity: 0.32; }
  50%       { opacity: 0.88; }
}

/* Hero */
.hero {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 3rem 0 2rem 0;
}

.logo-glow {
  position: relative;
  width: 210px;
  height: 210px;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 1.25rem;
}

.logo-glow::before {
  content: '';
  position: absolute;
  inset: -20px;
  border-radius: 50%;
  background: radial-gradient(circle, rgba(109,40,217,0.35) 0%, rgba(91,33,182,0.12) 55%, transparent 72%);
  animation: glow-pulse 3.5s ease-in-out 3 forwards;
}

@keyframes glow-pulse {
  0%,100% { transform: scale(1);    opacity: 0.55; }
  50%      { transform: scale(1.14); opacity: 1;   }
}

.logo-svg {
  width: 190px;
  height: 190px;
  position: relative;
  z-index: 1;
  animation: pop-in 0.9s cubic-bezier(0.16,1,0.3,1) both;
}

@keyframes pop-in {
  from { opacity: 0; transform: scale(0.8) translateY(8px); }
  to   { opacity: 1; transform: scale(1)   translateY(0);   }
}

.hb-line {
  stroke-dasharray: 600;
  stroke-dashoffset: 600;
  animation: draw-line 1.4s 0.3s cubic-bezier(0.4,0,0.2,1) forwards;
}

@keyframes draw-line {
  to { stroke-dashoffset: 0; }
}

.app-name {
  font-family: 'Inter Tight', sans-serif;
  font-size: 4.1rem;
  font-weight: 800;
  font-style: italic;
  letter-spacing: -0.045em;
  color: #ffffff;
  line-height: 1;
  text-align: center;
  animation: rise 0.7s 0.5s cubic-bezier(0.16,1,0.3,1) both;
}

.company-tag {
  font-family: 'Inter Tight', sans-serif;
  font-size: 0.72rem;
  font-weight: 300;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  color: var(--text-footer);
  margin-top: 0.5rem;
  text-align: center;
  animation: rise 0.7s 0.65s cubic-bezier(0.16,1,0.3,1) both;
}

@keyframes rise {
  from { opacity: 0; transform: translateY(14px); }
  to   { opacity: 1; transform: translateY(0);    }
}

/* Conversation */
.bubble-user {
  background: #12062a;
  border: 1px solid #2e1065;
  color: #c4b5fd;
  border-radius: 18px 18px 4px 18px;
  padding: 0.875rem 1.125rem;
  max-width: 86%;
  margin-left: auto;
  font-size: 0.91rem;
  line-height: 1.55;
  font-family: 'Inter Tight', sans-serif;
}

/* What the system took a follow-up to mean. Deliberately quiet: it is a
   disclosure, not a finding, and it sits between the patient's words and the
   answer so it reads in the order it happened.

   Carries a text label ("Answering about"), a border and an italic value, so it
   is never distinguished by colour alone. Colours are existing verified tokens
   rather than new ones -- a new token needs its own contrast entry, and reusing
   a measured pair is safer than adding an unmeasured one. Not interactive, so
   no touch-target floor applies; if it ever gains a control, it needs 44px. */
.rewrite-chip {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0.4rem;
  margin: 0.45rem 0 0.1rem auto;
  padding: 0.4rem 0.65rem;
  max-width: 86%;
  width: fit-content;
  background: var(--surface-inset);
  border: 1px solid var(--border-control);
  border-radius: 8px;
  font-size: 0.74rem;
  line-height: 1.5;
  font-family: 'Inter Tight', sans-serif;
  /* A long resolved query must wrap, not overflow, at 320px. */
  overflow-wrap: anywhere;
}
.rewrite-chip-label {
  color: var(--text-section);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  font-weight: 700;
  font-size: 0.62rem;
  flex-shrink: 0;
}
.rewrite-chip-value { color: var(--text-label-why); font-style: italic; }

.resp-wrap { display: flex; flex-direction: column; gap: 0.7rem; }

.resp-card {
  background: #0b0b1a;
  border: 1px solid var(--border-card);
  border-radius: 14px;
  padding: 0.95rem 1.1rem;
}

/* Section labels.
   These are real <h2> and <h3> elements so a screen reader can navigate the
   answer, which is what they are for. They are NOT visually headings: they are
   small uppercase labels.

   Streamlit styles headings inside its markdown container with a selector of
   the form `.st-emotion-cache-<hash> h2`, specificity (0,1,1), which beats a
   bare `.card-label` at (0,1,0). The result was 20px type with 16px of vertical
   padding on every label. The hash changes between Streamlit releases, so it
   cannot be targeted directly; !important is the honest tool here, and it is
   scoped to these two classes rather than applied to headings generally. */
h1.card-label, h2.card-label, h3.card-label, h4.card-label,
h1.ev-title, h2.ev-title, h3.ev-title, h4.ev-title,
h2.insufficient-heading, h3.insufficient-heading,
h2.emerg-title, h3.emerg-title {
  font-family: 'Inter Tight', sans-serif !important;
  padding: 0 !important;
  margin: 0 0 0.5rem 0 !important;
  line-height: 1.35 !important;
}

h1.card-label, h2.card-label, h3.card-label, h4.card-label {
  font-size: 0.62rem !important;
  font-weight: 700 !important;
  letter-spacing: 0.13em !important;
  text-transform: uppercase !important;
  color: var(--text-section) !important;
}

/* The remaining headings are genuine titles, but still nowhere near Streamlit's
   default heading scale. Sizes restated with the same weight for the same
   specificity reason. */
h1.ev-title, h2.ev-title, h3.ev-title, h4.ev-title {
  font-size: 0.86rem !important;
  font-weight: 600 !important;
  color: #ddd !important;
}
h2.insufficient-heading, h3.insufficient-heading {
  font-size: 0.95rem !important;
  font-weight: 700 !important;
}
h2.emerg-title, h3.emerg-title {
  font-size: 0.98rem !important;
  font-weight: 700 !important;
}

/* Limitations: a footnote, not a section. Same information, a fraction of the
   visual weight, and no card of its own. */
.limits-note {
  margin: 0.1rem 0 0 0;
  padding: 0.7rem 0.9rem;
  border-left: 2px solid var(--border-card);
  background: transparent;
}
.limits-note h2.limits-label,
.limits-note h3.limits-label {
  font-size: 0.58rem !important;
  color: var(--text-section) !important;
  margin: 0 0 0.3rem 0 !important;
}
.limits-list {
  margin: 0;
  padding-left: 1rem;
  list-style: disc;
}
.limits-list li {
  font-size: 0.76rem;
  color: var(--text-muted);
  line-height: 1.55;
  margin: 0.15rem 0;
}

.card-label {
  font-family: 'Inter Tight', sans-serif;
  font-size: 0.62rem;
  font-weight: 700;
  letter-spacing: 0.13em;
  text-transform: uppercase;
  margin-bottom: 0.5rem;
}

.card-body {
  font-size: 0.88rem;
  color: var(--text-body);
  line-height: 1.62;
  font-family: 'Inter Tight', sans-serif;
}

.q-card {
  background: #0b0b1a;
  border: 1px solid var(--border-card);
  border-radius: 14px;
  padding: 0.95rem 1.1rem;
}

.q-item {
  display: flex;
  gap: 0.7rem;
  align-items: flex-start;
  margin: 0.5rem 0;
  font-size: 0.88rem;
  color: var(--text-question);
  line-height: 1.5;
  font-family: 'Inter Tight', sans-serif;
}

.q-arrow { color: #7c3aed; font-weight: 700; flex-shrink: 0; }

.emerg-card {
  background: #140505;
  border: 1.5px solid #991b1b;
  border-radius: 14px;
  padding: 1.25rem;
  font-size: 0.88rem;
  color: #fca5a5;
  line-height: 1.65;
}

.emerg-title {
  font-family: 'Inter Tight', sans-serif;
  font-weight: 700;
  font-size: 0.98rem;
  color: var(--text-emergency-title);
  margin-bottom: 0.6rem;
}

.src-item {
  background: var(--surface-sunken);
  border: 1px solid var(--border-card);
  border-radius: 10px;
  padding: 0.75rem 1rem;
  margin: 0.4rem 0;
  font-size: 0.8rem;
}

.src-name { font-weight: 500; color: #ccc; margin-bottom: 0.15rem; }
.src-sub  { font-size: 0.72rem; color: var(--text-muted); }

.conf-bar  { height: 3px; background: #1a1a2a; border-radius: 2px; margin-top: 0.45rem; overflow: hidden; }
.conf-fill { height: 100%; border-radius: 2px; background: linear-gradient(90deg,#5b21b6,#8b5cf6); }

.turn-div { border: none; border-top: 1px solid #141424; margin: 1.25rem 0; }

/* ---------------------------------------------------------------------
   Accessibility foundations.
   --------------------------------------------------------------------- */

/* A visible focus indicator on every focusable thing, including Streamlit's
   own widgets. :focus-visible keeps it off for mouse clicks. The ring is
   measured against both the page and a card in synapse.a11y.palette. */
a:focus-visible,
button:focus-visible,
summary:focus-visible,
input:focus-visible,
textarea:focus-visible,
select:focus-visible,
[tabindex]:focus-visible,
.stButton > button:focus-visible,
.stTextArea textarea:focus-visible,
.stTextInput input:focus-visible,
.stMultiSelect div[data-baseweb="select"]:focus-within,
.stDownloadButton > button:focus-visible {
  outline: 3px solid var(--focus-ring) !important;
  outline-offset: 2px !important;
  border-radius: 6px;
}

/* Skip link: first thing in the tab order, visible once focused. Streamlit
   renders a lot of chrome before the form, and a keyboard user should not have
   to walk through it on every rerun. */
.skip-link {
  position: absolute;
  left: -9999px;
  top: 0;
  z-index: 100000;
  padding: 0.7rem 1rem;
  background: var(--surface-card);
  color: var(--text-primary);
  border: 2px solid var(--focus-ring);
  border-radius: 0 0 8px 0;
  font-size: 0.9rem;
  text-decoration: none;
}
.skip-link:focus {
  left: 0;
}

/* A collapsed sidebar is moved off-screen with a transform, but keeps
   visibility:visible, so every control inside it stays in the tab order. On a
   phone that means eight invisible stops before a keyboard user reaches
   anything they can see, which is a 2.4.3 and 2.4.7 failure: focus lands on
   things that are not perceivable.

   visibility:hidden removes descendants from the tab order, and Streamlit sets
   aria-expanded on the sidebar, so the rule reverses itself the moment the user
   opens it. The transition delay lets the slide-out animation finish before the
   contents become focusable again. */
[data-testid="stSidebar"][aria-expanded="false"] {
  visibility: hidden;
}
[data-testid="stSidebar"][aria-expanded="true"] {
  visibility: visible;
}

/* Minimum touch target, applied to Streamlit's controls as well as our own. */
.stButton > button,
.stDownloadButton > button,
.stTextInput input,
.stMultiSelect div[data-baseweb="select"] {
  min-height: 44px !important;
}

/* Screen-reader-only, kept in the accessibility tree. */
.visually-hidden {
  position: absolute !important;
  width: 1px; height: 1px;
  padding: 0; margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  clip-path: inset(50%);
  white-space: nowrap;
  border: 0;
}

/* Form labels are persistent and visible. The question field previously used
   a collapsed label with a placeholder standing in for it, which disappears
   the moment anyone types (requirements 13 and 14). */
.stTextArea label, .stTextInput label, .stMultiSelect label {
  display: block !important;
  visibility: visible !important;
  color: var(--text-primary) !important;
  font-size: 0.85rem !important;
  font-weight: 600 !important;
  margin-bottom: 0.3rem !important;
}

/* Reflow: no horizontal scrolling at 320px, and nothing that breaks at 200%
   zoom. Widths are relative and long strings wrap rather than overflow. */
/* overflow-x on <html> turns the document element into a scroll container,
   which assistive technology and axe both flag as a scrollable region with no
   keyboard access. Constraining the body instead keeps normal document
   scrolling, which is keyboard accessible by definition. */
body { overflow-x: hidden; }
.block-container { overflow-wrap: anywhere; }

/* Decorative motion. Everything below is ornament: it carries no information,
   so it is switched off entirely when the user asks for less motion. The
   selectors cover the streak elements, which used to carry inline animation
   styles that a media query could not reach. */
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
    scroll-behavior: auto !important;
  }
  .streak-decor { display: none !important; }
}

/* The in-app equivalent, applied when the reduce-motion setting is on. Some
   users cannot change an OS-level preference on a shared or clinic machine,
   which is the case the setting exists for. */
body.synapse-reduce-motion *,
body.synapse-reduce-motion *::before,
body.synapse-reduce-motion *::after {
  animation: none !important;
  transition: none !important;
}

/* Structured answer — one class per typed field rendered by
   synapse.answer.render. The container carries data-action, so the four action
   states are styleable (and inspectable) without parsing anything. */
.answer-view { display: flex; flex-direction: column; gap: 0.5rem; }

.claim { font-size: 0.88rem; color: var(--text-body); line-height: 1.74; margin: 0.35rem 0; }
.cite  { color: var(--text-accent); font-weight: 700; font-size: 0.8rem; }

/* A partially-supported claim is displayed, per the P1 display policy, but it
   is never displayed unmarked. */
.partial-flag {
  color: var(--text-caution);
  font-size: 0.72rem;
  font-weight: 600;
  white-space: nowrap;
}

.staff-card {
  background: #14100a;
  border: 1.5px solid #92400e;
  border-radius: 14px;
  padding: 1.1rem 1.25rem;
  font-size: 0.88rem;
  color: #fcd34d;
  line-height: 1.65;
}

.abstain-card {
  background: #0b0b1a;
  border: 1px dashed var(--border-card);
  border-radius: 14px;
  padding: 1.1rem 1.25rem;
}
.abstain-card .card-label { color: var(--text-section); }

/* The permanent disclaimer. Rendered from a module constant on every answer,
   including abstentions, escalations and failures. */
.disclaimer-permanent {
  font-size: 0.74rem;
  color: var(--text-disclaimer);
  line-height: 1.6;
  padding: 0.7rem 0.9rem;
  border-left: 3px solid var(--border-control);
  background: var(--surface-deep);
  border-radius: 0 8px 8px 0;
}

/* Evidence cards: the verbatim spans behind each displayed claim. */
.ev-card {
  background: #0a0a18;
  border: 1px solid #181828;
  border-radius: 10px;
  padding: 0.8rem 1rem;
  margin: 0.4rem 0;
}
.ev-quote  { margin: 0.5rem 0; font-size: 0.8rem; }
.ev-source { color: var(--text-label-why); font-size: 0.78rem; }
.ev-text {
  margin: 0.3rem 0 0.2rem 0;
  padding-left: 0.7rem;
  border-left: 3px solid var(--border-control);
  color: var(--text-quote);
  font-style: italic;
  line-height: 1.6;
}

.src-empty { color: var(--text-muted); font-size: 0.82rem; padding: 0.5rem 0; }

/* ---------------------------------------------------------------------
   Evidence experience — mobile-first, keyboard-operable, no hover-only.

   The disclosure is a native <details>/<summary>: keyboard operable, exposes
   its expanded state to assistive technology without an aria attribute anyone
   can forget to update, and works with no JavaScript (which Streamlit's
   markdown sanitiser would strip anyway).

   Everything below wraps rather than truncates. A long journal name or a
   verbatim excerpt must stay readable at 320px, which is the narrowest phone
   still in common use.
   --------------------------------------------------------------------- */

.claim-block { margin: 0.4rem 0 0.9rem 0; }

/* Screen-reader-only text: available to assistive technology, invisible on
   screen. The clip-path form is used rather than display:none, which would
   remove it from the accessibility tree entirely. */
.visually-hidden {
  position: absolute !important;
  width: 1px; height: 1px;
  padding: 0; margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  clip-path: inset(50%);
  white-space: nowrap;
  border: 0;
}

.ev-details { margin: 0.35rem 0 0 0; }

.ev-summary {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  cursor: pointer;
  list-style: none;
  padding: 0.55rem 0.7rem;
  min-height: 44px;              /* Touch target floor */
  border: 1px solid var(--border-control);
  border-radius: 10px;
  background: var(--surface-sunken);
  color: var(--text-question);
  font-size: 0.82rem;
  font-family: 'Inter Tight', sans-serif;
  transition: border-color 0.15s, background 0.15s;
}
.ev-summary::-webkit-details-marker { display: none; }

/* A visible, high-contrast focus ring. Keyboard users must be able to see
   where they are; :focus-visible keeps it off for mouse clicks. */
.ev-summary:focus-visible {
  outline: 3px solid #a78bfa;
  outline-offset: 2px;
  border-color: #a78bfa;
}
.ev-summary:hover { border-color: #4c1d95; background: #0d0d1f; }

/* The disclosure triangle is drawn from the element's own open state, so the
   visual and the semantic state cannot drift apart. */
.ev-summary::before {
  content: '▸';
  color: #8b5cf6;
  flex-shrink: 0;
  transition: transform 0.15s;
}
.ev-details[open] > .ev-summary::before { content: '▾'; }
.ev-details[open] > .ev-summary { border-color: #4c1d95; border-radius: 10px 10px 0 0; }

.ev-summary-text { flex: 1 1 auto; color: var(--text-question); }
.ev-summary-marker { color: #8b5cf6; font-weight: 600; flex-shrink: 0; }

.ev-cards {
  border: 1px solid var(--border-control);
  border-top: none;
  border-radius: 0 0 10px 10px;
  padding: 0.6rem;
  background: #080814;
}

.ev-card {
  background: var(--surface-card);
  border: 1px solid var(--border-card);
  border-radius: 10px;
  padding: 0.85rem;
  margin: 0.5rem 0;
  /* Long identifiers and unbroken strings must wrap, not overflow. */
  overflow-wrap: anywhere;
  word-break: break-word;
}

.ev-title {
  font-size: 0.86rem;
  font-weight: 600;
  color: #ddd;
  line-height: 1.45;
  margin-bottom: 0.5rem;
}
.ev-number { color: #8b5cf6; margin-right: 0.3rem; }

.ev-section-label {
  font-size: 0.6rem;
  font-weight: 700;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--text-disclaimer);
  margin: 0.5rem 0 0.3rem 0;
}

.ev-quote {
  margin: 0.3rem 0;
  padding: 0.5rem 0.7rem;
  border-left: 3px solid #4c1d95;
  background: #0d0d1f;
  border-radius: 0 6px 6px 0;
  color: #b9b9c6;
  font-size: 0.83rem;
  line-height: 1.65;
  font-style: italic;
}
.ev-chunk {
  display: block;
  margin-top: 0.35rem;
  font-style: normal;
  font-size: 0.72rem;
  color: var(--text-muted);
}

/* Definition list: stacked on a phone, two columns once there is room. */
.ev-meta { margin: 0.6rem 0 0 0; font-size: 0.76rem; }
.ev-row { display: flex; flex-direction: column; gap: 0.1rem; padding: 0.25rem 0; border-top: 1px solid #14142a; }
.ev-row dt { color: var(--text-section); font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em; }
.ev-row dd { color: #b9b9c6; margin: 0; }
@media (min-width: 420px) {
  .ev-row { flex-direction: row; gap: 0.75rem; }
  .ev-row dt { flex: 0 0 8.5rem; }
  .ev-row dd { flex: 1 1 auto; }
}

.ev-labels { display: flex; flex-direction: column; gap: 0.4rem; margin-top: 0.7rem; }
.ev-label {
  padding: 0.45rem 0.6rem;
  border-radius: 8px;
  background: var(--surface-inset);
  border: 1px solid var(--border-card);
}
.ev-label-name { display: block; font-size: 0.76rem; font-weight: 600; color: var(--text-label); }
/* The explanation is always-visible text, never a title tooltip: a tooltip is
   hover-only and invisible on a touch screen. */
.ev-label-why { display: block; margin-top: 0.2rem; font-size: 0.74rem; color: var(--text-label-why); line-height: 1.5; }
.ev-tone-caution { border-color: #4a3a10; background: #14100a; }
.ev-tone-caution .ev-label-name { color: var(--text-caution); }

/* Retrieval relevance sits apart from the governance labels: it is a search
   score, not a judgement about the evidence. */
.ev-relevance { margin-top: 0.6rem; padding: 0.45rem 0.6rem; border-radius: 8px; background: #080814; border: 1px dashed var(--border-control); }
.ev-relevance-value { display: block; font-size: 0.74rem; color: var(--text-label-why); }

.ev-links { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 0.7rem; }
.ev-link {
  display: inline-flex;
  align-items: center;
  min-height: 44px;
  padding: 0.35rem 0.7rem;
  border: 1px solid #2e1065;
  border-radius: 8px;
  color: #a78bfa !important;
  text-decoration: none;
  font-size: 0.76rem;
}
.ev-link:focus-visible { outline: 3px solid #a78bfa; outline-offset: 2px; }
.ev-link:hover { background: rgba(109,40,217,0.13); }

.ev-pack { margin-top: 0.6rem; font-size: 0.72rem; color: var(--text-muted); line-height: 1.5; }
.ev-pack-id { color: var(--text-label-why); }
.ev-empty { color: var(--text-muted); font-size: 0.82rem; }

/* Insufficient evidence — a designed state, not an error card. Deliberately
   not red: the system having no citation is an ordinary limitation, and
   styling it as a fault invites a reader to think their question was alarming. */
.insufficient-card {
  background: #0b0b1a;
  border: 1px solid #2a2a45;
  border-left: 3px solid #6d28d9;
  border-radius: 14px;
  padding: 1.1rem 1.25rem;
  margin: 0.5rem 0;
}
.insufficient-heading { font-size: 0.95rem; font-weight: 700; color: #ddd; margin-bottom: 0.5rem; line-height: 1.4; }
.insufficient-message { font-size: 0.88rem; color: var(--text-body); line-height: 1.7; }
.insufficient-caveat { margin-top: 0.6rem; font-size: 0.84rem; color: var(--text-label-why); line-height: 1.65; }
.insufficient-steps { margin: 0.4rem 0 0 1.1rem; padding: 0; }
.insufficient-steps li { font-size: 0.85rem; color: var(--text-question); line-height: 1.6; margin: 0.35rem 0; }

/* Respect a reduced-motion preference across every animation on the page. */
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
  }
}

/* Disclaimer */
.disclaimer {
  text-align: center;
  font-size: 0.72rem;
  color: var(--text-footer);
  letter-spacing: 0.02em;
  line-height: 1.7;
  padding: 0.75rem 0.5rem 0.6rem 0.5rem;
  border-top: 1px solid #14142a;
  margin-bottom: 0.4rem;
  animation: rise 0.7s 0.8s both;
  font-family: 'Inter Tight', sans-serif;
}

.not-red { color: #8b0000; font-weight: 700; }

/* Input */
/* The rule that used to live here hid every Streamlit form label with
   display:none, which removes it from the accessibility tree. Labels are now
   visible and persistent; see the accessibility block above. */
.stTextArea textarea {
  background: #090916 !important;
  border: 1px solid #1e1e38 !important;
  border-radius: 14px !important;
  color: #e0e0e0 !important;
  font-family: 'Inter Tight', sans-serif !important;
  font-size: 0.93rem !important;
  padding: 0.9rem 1rem !important;
  resize: none !important;
}
.stTextArea textarea:focus {
  border-color: #5b21b6 !important;
  box-shadow: 0 0 0 3px rgba(91,33,182,0.14) !important;
}
.stTextArea textarea::placeholder { color: #3a3a5a !important; }

/* Streamlit prints "Press ⌘+Enter to submit form" inside a form's text area.
   That instruction is now FALSE: the binding above sends on plain Enter and
   reserves Shift+Enter for a new line. Leaving it would teach a shortcut that
   is no longer the one that works, and display:none removes it from the
   accessibility tree as well as the screen — which is correct here, because a
   wrong instruction read aloud is worse than no instruction. The true one is in
   the field's help text, which is properly associated with the input.

   Scoped to the form on purpose. The appointment brief has its own text areas
   outside it, where Streamlit's hint still describes real behaviour. Targeted
   by data-testid because Streamlit's class names are hashed and change between
   releases. */
[data-testid="stForm"] [data-testid="InputInstructions"] { display: none; }

.stButton > button {
  background: transparent !important;
  color: #fff !important;
  border: 1.5px solid var(--border-button) !important;
  border-radius: 12px !important;
  padding: 0.72rem 2rem !important;
  font-family: 'Inter Tight', sans-serif !important;
  font-size: 0.88rem !important;
  font-weight: 700 !important;
  letter-spacing: 0.05em !important;
  width: 100% !important;
  transition: background 0.2s, border-color 0.2s, transform 0.1s !important;
}
.stButton > button:hover {
  background: rgba(109,40,217,0.13) !important;
  border-color: #8b5cf6 !important;
  transform: translateY(-1px) !important;
}
.stButton > button:active { transform: translateY(0) !important; }

/* Question-processing progress */
[data-testid="stProgress"] {
  margin: 0.85rem 0 0.35rem 0;
}
[data-testid="stProgress"] [data-baseweb="progress-bar"] > div > div {
  background-color: #ffffff !important;
}
[data-testid="stProgress"] [data-baseweb="progress-bar"] > div > div > div {
  background: #7c3aed !important;
  box-shadow: none !important;
  transition: transform 0.5s linear !important;
}
[data-testid="stProgress"] p {
  color: #ffffff !important;
  font-family: 'Inter Tight', sans-serif !important;
  font-size: 0.78rem !important;
  letter-spacing: 0.02em;
}

[data-testid="stSidebar"] { background: #060614 !important; border-right: 1px solid #16162a !important; }

.empty-hint {
  text-align: center;
  color: var(--text-footer);
  font-size: 0.85rem;
  line-height: 1.8;
  padding: 1.25rem 0 1.75rem 0;
  animation: rise 0.7s 0.9s both;
  font-family: 'Inter Tight', sans-serif;
}
</style>
""", unsafe_allow_html=True)

# Decorative edge streaks. Presentational only, so they are hidden from
# assistive technology and removed entirely under reduced motion. They carry a
# class rather than inline animation styles: an inline style cannot be
# overridden by a prefers-reduced-motion media query.
st.markdown("""
<style>
@keyframes streak-breathe-real {
  0%, 100% { opacity: 0.32; }
  50%       { opacity: 0.92; }
}
.streak-decor {
  position: fixed; bottom: -30px;
  width: 4px; height: 320px;
  background: linear-gradient(to top, rgba(167,139,250,1) 0%, rgba(139,92,246,0.75) 35%, rgba(109,40,217,0.35) 65%, transparent 100%);
  box-shadow: 0 0 22px 7px rgba(139,92,246,0.75), 0 0 65px 18px rgba(109,40,217,0.4);
  border-radius: 3px; pointer-events: none; z-index: 99999;
  animation: streak-breathe-real 3.5s ease-in-out infinite;
}
.streak-decor.left  { left: 38px;  transform: rotate(-20deg); transform-origin: bottom center; }
.streak-decor.right { right: 38px; transform: rotate(20deg);  transform-origin: bottom center; animation-delay: 0.7s; }
</style>
<div class="streak-decor left" aria-hidden="true"></div>
<div class="streak-decor right" aria-hidden="true"></div>
""", unsafe_allow_html=True)
# AUDIO REMOVED 2026-08-21.
#
# A Web Audio heartbeat used to be injected here, scheduled to play three beats
# in time with the logo animation on every session. The accessibility audit
# established it never actually sounded: Streamlit renders markdown through
# React's dangerouslySetInnerHTML, and a <script> inserted via innerHTML does
# not execute. It was verified in a real browser rather than assumed.
#
# It is deleted rather than left dormant. The intent to autoplay audio was in
# the source, a Streamlit custom component would have made it work, and
# unsolicited sound is a WCAG 1.4.2 failure that also startles anyone using a
# screen reader. There is now no audio in Synapse at all, so there is nothing to
# mute and no opt-in to build (requirements 1-3).


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

def init():
    # `conversation` holds {"query": str, "outcome": synapse.ui.TurnOutcome}.
    # The outcome is a typed object, not a dict of free text: nothing in this
    # file may re-derive structure from prose.
    for k, v in {
        "conversation": [], "chunks_built": False, "chunks": [],
        "hybrid": None, "api_key": os.getenv("OPENAI_API_KEY", ""),
    }.items():
        if k not in st.session_state:
            st.session_state[k] = v

init()


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown("### Settings")

    # In-app reduced motion (requirement 5). The OS-level preference is honoured
    # automatically through a media query; this exists for people who cannot
    # change that setting, which on a shared or clinic machine is common.
    st.checkbox(
        "Reduce motion",
        key="reduce_motion",
        help="Turns off the background animation and all transitions.",
    )
    st.caption(
        "Synapse plays no sound. If your device is set to reduce motion, that is "
        "already respected without changing anything here."
    )
    key_in = st.text_input("OpenAI API Key", type="password", placeholder="sk-...")
    if key_in:
        st.session_state.api_key = key_in
    st.markdown("---")
    st.markdown("### 📊 Metrics")
    # Reads the most recent evaluation run's summary.json rather than computing
    # metrics inline. Live user queries have no ground truth, so scoring them
    # produced structurally-zero numbers in the previous implementation.
    # Produce a summary with:  python -m synapse.evals.run --offline ...
    import json as _json          # Hoisted OUT of the try: the except clause below references _json.JSONDecodeError, which would raise NameError if the import were still inside the block it guards
    from pathlib import Path as _Path

    try:
        _summaries = sorted(_Path("artifacts/evals").glob("*/summary.json")) if _Path("artifacts/evals").is_dir() else []
        if _summaries:
            _s = _json.loads(_summaries[-1].read_text(encoding="utf-8"))
            _headline = _s.get("headline_metrics", {})
            for _name, _label in (("recall_at_5", "Recall@5"), ("ndcg_at_5", "nDCG@5"), ("emergency_sensitivity", "Emergency recall")):
                _m = _headline.get(_name)
                if _m and _m.get("value") is not None:
                    # The denominator travels with the number: "100%" over one
                    # case must not read like "100%" over two hundred.
                    st.metric(_label, f"{_m['value']:.0%}", help=f"n={_m.get('denominator', 0)}")
            if not _s.get("release_gating_capable", False):
                st.caption("⚠️ No case in that run is release-gating. Informational only.")
        else:
            st.caption("No evaluation run found. See docs/evaluation-metrics.md.")
    except (OSError, _json.JSONDecodeError) as _exc:
        # NARROWED from `except Exception` during the final audit. Two separate
        # defects were fixed here:
        #   1. The blanket catch hid every failure mode — a schema change, a
        #      permission error and a truncated file all looked identical.
        #   2. The message was false. Metrics do NOT "appear after first query";
        #      they are produced by an OFFLINE evaluation run against a fixed
        #      dataset. A patient query has no ground truth to score against,
        #      which is precisely why the previous inline scorer was removed.
        # Only the two errors that legitimately mean "no readable summary yet"
        # are caught. Anything else propagates, as it should.
        st.caption(f"Could not read the evaluation summary ({type(_exc).__name__}).")
    st.markdown("---")
    st.caption("Synapse searches PubMed research to help you prepare for your doctor visit. Not a diagnostic tool.")


# ---------------------------------------------------------------------------
# Hero
# ---------------------------------------------------------------------------

# Reduced-motion setting (requirement 5). Emitted before anything renders, so
# the page is still on the first paint rather than settling after a flash of
# animation.
#
# The rules live in a constant rather than a literal <style> block, because the
# snapshot harness extracts every <style> it finds in this file. As a literal it
# was captured unconditionally, which silently disabled motion in every
# committed snapshot and made the "does anything animate?" test vacuous.
REDUCED_MOTION_CSS = (
    "*, *::before, *::after {"
    " animation: none !important;"
    " transition: none !important;"
    " scroll-behavior: auto !important;"
    " }"
    " .streak-decor { display: none !important; }"
)

if st.session_state.get("reduce_motion"):
    st.markdown(f"<style>{REDUCED_MOTION_CSS}</style>", unsafe_allow_html=True)

# The skip link is the first focusable element on the page.
st.markdown(
    '<a class="skip-link" href="#ask-synapse">Skip to the question box</a>',
    unsafe_allow_html=True,
)

st.markdown("""
<div class="hero">
  <div class="logo-glow" aria-hidden="true">
    <svg class="logo-svg" role="presentation" viewBox="0 0 200 200" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <linearGradient id="lg" x1="0%" y1="0%" x2="100%" y2="0%">
          <stop offset="0%"   stop-color="#2e1065" stop-opacity="0.8"/>
          <stop offset="35%"  stop-color="#6d28d9"/>
          <stop offset="65%"  stop-color="#8b5cf6"/>
          <stop offset="100%" stop-color="#ddd6fe" stop-opacity="0.9"/>
        </linearGradient>
        <filter id="gl">
          <feGaussianBlur stdDeviation="2" result="b"/>
          <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
        </filter>
      </defs>
      <polyline class="hb-line"
        points="8,100 65,100 78,100 92,36 108,164 122,100 138,100 192,100"
        fill="none"
        stroke="url(#lg)"
        stroke-width="4"
        stroke-linecap="round"
        stroke-linejoin="round"
        filter="url(#gl)"
      />
    </svg>
  </div>
  <h1 class="app-name">SYNAPSE</h1>
  <p class="company-tag">A Zenith Company</p>
</div>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Response renderer
#
# Typed fields only. This function reads summary, claims, doctor_evaluation,
# questions_for_doctor, limitations and action off a validated GroundedAnswer
# and hands them to synapse.answer.render, which escapes every value and emits
# the disclaimer from a constant.
#
# REPLACED: the previous implementation recovered structure by running three
# regexes over free-form model prose (``re.search(r'📋 WHAT THE RESEARCH
# SAYS...')``) and interpolated the result into HTML with
# unsafe_allow_html=True. A reordered or truncated generation silently dropped a
# section — including, when truncated, the disclaimer — and the patient's query,
# the model's output and PubMed-derived titles all reached the page unescaped.
# ---------------------------------------------------------------------------

def _resolved_query(outcome):
    """The rewritten retrieval query for this turn, or "" if there was none.

    Reads the nested `rewrite` block off the retrieval bundle. Every step is
    defensive because the turn may be a failure (no presentation at all), may
    predate this field, or may have come through the legacy conversion path,
    and a missing disclosure must never cost the patient their answer.
    """
    try:
        if not outcome.ok:
            return ""
        metadata = getattr(outcome.presentation.conversion, "metadata", None)
        if not isinstance(metadata, dict):
            return ""
        rewrite = metadata.get("rewrite")
        if not isinstance(rewrite, dict):
            return ""
        resolved = rewrite.get("resolved_query")
        return resolved if isinstance(resolved, str) else ""
    except Exception:
        return ""


def render_turn(index, turn):
    """Render one conversation turn: the question, then the outcome."""
    from synapse.answer.render import (
        escape,
        render_answer_html,
        render_evidence_html,
        render_failure_html,
        render_sources_html,
    )
    from synapse.answer.render import PERMANENT_DISCLAIMER
    from synapse.answer.schema import AnswerAction
    from synapse.evidence import (
        InsufficientEvidence,
        InsufficientReason,
        from_failure_code,
        render_insufficient_html,
    )
    from synapse.ui.errors import PATIENT_ERROR_MESSAGE

    # Failure codes that mean "no verified evidence" rather than "the tool
    # broke". Anything else is a fault and gets the failure card.
    EVIDENCE_FAILURE_CODES = {"evidence_unavailable", "index_unverified"}

    def _permanent_disclaimer_html() -> str:
        """The permanent disclaimer, shown in the insufficient-evidence state too."""
        return f'<div class="disclaimer-permanent">{escape(PERMANENT_DISCLAIMER)}</div>'

    # The patient's own words, escaped. Previously interpolated raw.
    st.markdown(f'<div class="bubble-user">{escape(turn["query"])}</div>', unsafe_allow_html=True)

    outcome = turn["outcome"]
    # What the system took a follow-up to mean, shown ONLY when a rewrite
    # actually happened. Silence is the normal case: on a standalone question
    # there is nothing to disclose and a chip on every turn would become
    # furniture nobody reads.
    #
    # This is model-generated text, so it goes through escape() like every other
    # dynamic value. It is a plain paragraph, not a live region: a search term
    # is not urgent and must not interrupt a screen reader mid-answer.
    resolved_query = _resolved_query(outcome)
    if resolved_query:
        st.markdown(
            f'<p class="rewrite-chip"><span class="visually-hidden">Searched for: </span>'
            f'<span class="rewrite-chip-label">Answering about</span>'
            f'<span class="rewrite-chip-value">{escape(resolved_query)}</span></p>',
            unsafe_allow_html=True,
        )

    if not outcome.ok:
        code = outcome.failure.code.value
        if code in EVIDENCE_FAILURE_CODES:
            # Not an error to a patient: the system has no verified evidence for
            # this question. A first-class designed state, with fixed copy that
            # no model contributes to.
            st.markdown(
                render_insufficient_html(
                    InsufficientEvidence(
                        from_failure_code(code),
                        pack_id=RESOLVER.pack_id,
                        pack_version=RESOLVER.pack_version,
                    )
                ),
                unsafe_allow_html=True,
            )
            st.markdown(_permanent_disclaimer_html(), unsafe_allow_html=True)
        else:
            # A genuine fault: a fixed message and a typed reference code. No
            # provider exception, and no fallback to unvalidated text.
            st.markdown(
                render_failure_html(PATIENT_ERROR_MESSAGE, code),
                unsafe_allow_html=True,
            )
        return

    answer = outcome.presentation.answer
    numbering = outcome.presentation.numbering

    if answer.action is AnswerAction.ABSTAIN:
        # Abstention IS the insufficient-evidence state: the display policy
        # withheld every claim, so the page says so in those words rather than
        # showing an answer card with nothing in it.
        st.markdown(
            render_insufficient_html(
                InsufficientEvidence(
                    InsufficientReason.BELOW_THRESHOLD,
                    pack_id=RESOLVER.pack_id,
                    pack_version=RESOLVER.pack_version,
                )
            ),
            unsafe_allow_html=True,
        )

    # Every dynamic value below was escaped by the renderer before it got here.
    st.markdown(render_answer_html(answer, numbering, resolver=RESOLVER), unsafe_allow_html=True)

    if answer.action is AnswerAction.EMERGENCY:
        # Nothing was retrieved and nothing was generated: an escalation cites
        # no sources and offers no takeaway to download.
        return

    if answer.claims:
        with st.expander("Supporting excerpts", expanded=False):
            # The verbatim spans the verifier matched, under the same source
            # numbers used inline.
            st.markdown(render_evidence_html(answer, numbering), unsafe_allow_html=True)

    with st.expander("Research sources", expanded=False):
        st.markdown(render_sources_html(numbering), unsafe_allow_html=True)

    _render_brief_editor(index, turn, answer, numbering)


def _render_brief_editor(index, turn, answer, numbering):
    """The appointment brief: review, edit, choose sections, then export.

    Nothing is written to disk or sent anywhere. Every export is an explicit
    click that hands bytes to the browser's download, and the privacy warning is
    shown before the buttons rather than after.
    """
    from synapse.brief import (
        EXPORT_WARNING,
        BriefSection,
        UserContent,
        add_question,
        build_brief,
        build_export,
        estimate_fit,
        overflow_advice,
        remove_question,
        reorder_questions,
        set_notes,
        set_sections,
        set_topic,
    )

    state_key = f"brief-{index}"

    with st.expander("Appointment brief", expanded=False):
        # Built once per turn and kept in session memory only. Re-running the
        # build would issue a new document identifier on every interaction.
        if state_key not in st.session_state:
            st.session_state[state_key] = build_brief(
                answer,
                numbering,
                resolver=RESOLVER,
                user=UserContent(topic=turn["query"].strip()[:200]),
                app_version="0.1.0",
                corpus_version=st.session_state.get("corpus_version", ""),
                index_version=st.session_state.get("index_version", ""),
            )
        brief = st.session_state[state_key]

        st.caption(
            "Everything below is yours to change. Anything you write or edit is "
            "marked as yours, and edited statements stop being labelled as checked "
            "against a source."
        )

        topic = st.text_input(
            "What I want to talk about", value=brief.user.topic, key=f"{state_key}-topic"
        )
        notes = st.text_area(
            "My notes (optional)", value=brief.user.notes, key=f"{state_key}-notes", height=80
        )
        if topic != brief.user.topic:
            brief = set_topic(brief, topic)
        if notes != brief.user.notes:
            brief = set_notes(brief, notes)

        # Questions: reorder by editing the list, add your own, remove any.
        if brief.questions:
            st.markdown("**Questions to ask**")
            order = st.multiselect(
                "Order and selection (drag to reorder)",
                options=[question.question_id for question in brief.questions],
                default=[question.question_id for question in brief.questions],
                format_func=lambda qid: next(
                    q.text for q in brief.questions if q.question_id == qid
                ),
                key=f"{state_key}-order",
            )
            if order and set(order) != {q.question_id for q in brief.questions}:
                for question in list(brief.questions):
                    if question.question_id not in order:
                        brief = remove_question(brief, question.question_id)
            if order and len(order) == len(brief.questions):
                brief = reorder_questions(brief, order)

        own_question = st.text_input("Add your own question", key=f"{state_key}-newq")
        if own_question.strip() and st.button("Add question", key=f"{state_key}-addq"):
            brief = add_question(brief, own_question)

        chosen = st.multiselect(
            "Sections to include",
            options=[section.value for section in BriefSection],
            default=[section.value for section in brief.included_sections],
            key=f"{state_key}-sections",
            help="The medical disclaimer is always included and cannot be removed.",
        )
        brief = set_sections(brief, [BriefSection(value) for value in chosen])
        st.session_state[state_key] = brief

        # One-page fit: measured and reported, never silently shrunk.
        fit = estimate_fit(brief)
        if not fit.fits:
            st.warning(
                f"This is about {fit.fill_ratio:.0%} of one page. "
                "Trim something below, or print it on two pages."
            )
            for suggestion in overflow_advice(brief):
                st.caption(f"• {suggestion}")

        st.info(EXPORT_WARNING, icon="🔒")

        bundle = build_export(brief)
        sizes = bundle.sizes()
        left, middle, right = st.columns(3)
        with left:
            st.download_button(
                "Print / PDF",
                data=bundle.html,
                file_name=bundle.html_filename,
                mime="text/html",
                key=f"{state_key}-html",
                help=f"Open and print, or save as PDF ({sizes['html'] // 1024} KB).",
            )
        with middle:
            st.download_button(
                "Plain text",
                data=bundle.text,
                file_name=bundle.text_filename,
                mime="text/plain",
                key=f"{state_key}-text",
            )
        with right:
            st.download_button(
                "JSON",
                data=bundle.json_text,
                file_name=bundle.json_filename,
                mime="application/json",
                key=f"{state_key}-json",
                help="Structured copy, for moving the brief to another tool.",
            )
        st.caption(
            "Files download to your device. Synapse does not save them, upload them, "
            "or send them anywhere."
        )


# ---------------------------------------------------------------------------
# Conversation
# ---------------------------------------------------------------------------

if st.session_state.conversation:
    st.markdown(
        '<h2 class="visually-hidden">Your questions and answers</h2>', unsafe_allow_html=True
    )
    for _index, _turn in enumerate(st.session_state.conversation):
        render_turn(_index, _turn)
        st.markdown('<hr class="turn-div">', unsafe_allow_html=True)
else:
    st.markdown("""<div class="empty-hint">
      Describe your symptoms or ask anything<br>you want to understand before seeing your doctor.
    </div>""", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Disclaimer + input
#
# This banner is the application's own, rendered on every load regardless of
# what any answer contains. The per-answer permanent disclaimer is separate and
# comes from synapse.answer.render.
# ---------------------------------------------------------------------------

st.markdown("""
<div class="disclaimer">
  This is <span class="not-red">NOT</span> a diagnosis tool.<br>
  Please consult your physician for emergencies.
</div>
""", unsafe_allow_html=True)

# The form target for the skip link, and the anchor focus returns to.
st.markdown('<div id="ask-synapse"></div>', unsafe_allow_html=True)

with st.form("q_form", clear_on_submit=True):
    # A persistent visible label, not a placeholder. The previous version used
    # label_visibility="collapsed" with the placeholder standing in for the
    # label, which vanishes as soon as anyone types and is announced
    # inconsistently by screen readers (requirements 13 and 14).
    query = st.text_area(
        "Your question or symptoms",
        placeholder="For example: why does my blood sugar go up overnight?",
        height=88,
        help=(
            "Describe what you want to understand before your appointment. Plain language "
            "is fine. Press Enter to send; Shift+Enter starts a new line."
        ),
    )
    submitted = st.form_submit_button("Ask Synapse")

# Enter sends, Shift+Enter starts a new line.
#
# Streamlit's own binding for a text area inside a form is the reverse: Enter
# inserts a newline and only Cmd/Ctrl+Enter submits. `enter_to_submit` on
# st.form does not change it -- verified in a real browser -- because the text
# area handles the key before the form ever sees it.
#
# This runs through components.html rather than st.markdown DELIBERATELY. A
# <script> injected with unsafe_allow_html never executes: Streamlit renders
# markdown through React's dangerouslySetInnerHTML, which does not run scripts.
# That is the same mistake the removed audio feature made, and it is documented
# above. A component is a real iframe, so its script runs, and it is same-origin
# with the host page, so it can reach window.parent.document -- both verified in
# a browser rather than assumed.
st.components.v1.html(
    """
<script>
(function () {
  var doc;
  try { doc = window.parent.document; } catch (e) { return; }  // Cross-origin: leave the default binding alone
  if (doc.__synapseEnterToSend) { return; }                    // Streamlit reruns this on every render
  doc.__synapseEnterToSend = true;

  doc.addEventListener("keydown", function (event) {
    if (event.key !== "Enter" || event.shiftKey) { return; }   // Shift+Enter falls through to a newline
    if (event.isComposing || event.keyCode === 229) { return; } // Mid-IME composition: Enter commits the candidate
    var target = event.target;
    if (!target || target.tagName !== "TEXTAREA") { return; }
    // Only the question box. The appointment brief has its own text areas, and
    // Enter must still insert a newline in those.
    if (target.getAttribute("aria-label") !== "Your question or symptoms") { return; }

    var submit = null;
    var buttons = doc.querySelectorAll("button");
    for (var i = 0; i < buttons.length; i++) {
      if ((buttons[i].innerText || "").trim() === "Ask Synapse") { submit = buttons[i]; break; }
    }
    if (!submit || submit.disabled) { return; }  // No button, no interception: never trap the key with nowhere to send it

    event.preventDefault();
    event.stopPropagation();
    submit.click();
  }, true);  // Capture phase, so this runs before the text area inserts a newline
})();
</script>
""",
    height=0,
)

# ---------------------------------------------------------------------------
# Submit handler
#
# Ordering lives in synapse.ui.pipeline, not here: emergency check, then
# retrieval, then schema-constrained generation, then verification, then the
# display decision. This block supplies the three injection points (retrieval,
# generation client, red-flag check) and drives the progress bar.
# ---------------------------------------------------------------------------

def run_with_progress(progress_bar, task, start, ceiling, label):
    """Run a blocking pipeline stage while moving the UI progress bar smoothly.

    ``label`` may be a string or a zero-argument callable, so a task that moves
    through several stages can report which one it is on without touching
    Streamlit from the worker thread. Every progress_bar call below happens on
    the main thread.
    """
    value = start
    progress_bar.progress(value, text=label() if callable(label) else label)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(task)
        while True:
            try:
                return future.result(timeout=0.55)
            except FutureTimeoutError:
                # Match Streamlit's built-in 0.5s transition so each update
                # completes before the next one begins.
                value += (ceiling - value) * 0.16
                progress_bar.progress(value, text=label() if callable(label) else label)

if submitted and query.strip():
    if not st.session_state.api_key:
        st.error("Add your OpenAI API key in the sidebar.")
        st.stop()

    api_key = st.session_state.api_key
    # Requirement 15: a polite live region for the loading state. It is polite,
    # not assertive: a progress update must not interrupt what the user is
    # reading. The emergency card carries role="alert" instead, which is the one
    # case that should interrupt.
    status_slot = st.empty()
    status_slot.markdown(
        '<div role="status" aria-live="polite" class="visually-hidden">'
        "Checking your question. This usually takes a few seconds.</div>",
        unsafe_allow_html=True,
    )
    loading = st.progress(5, text="Checking your question...")

    from synapse.answer.render import escape
    from synapse.answer.providers import DEFAULT_MODEL, OpenAIStructuredClient
    from synapse.answer.generate import PROMPT_ID as ANSWER_PROMPT_ID
    from synapse.answer.schema import AnswerAction
    from synapse.memory.query_rewrite import TurnSummary
    from synapse.retrieval.rerank import PROMPT_ID as RERANK_PROMPT_ID
    from synapse.safety import load_detector
    from synapse.ui.pipeline import answer_turn

    # The prompt carries its own version, so a wording change is visible in
    # telemetry even when the schema is unchanged.
    SYNAPSE_PROMPT_VERSION = ANSWER_PROMPT_ID

    stage = {"label": "Checking your question..."}       # Read by the progress loop on the main thread
    cached_chunks = st.session_state.chunks              # Read here, on the main thread, not inside the worker
    cached_hybrid = st.session_state.hybrid

    # Source-pack governance, resolved here because it reads files and the
    # retrieve closure runs on a worker.
    #
    # Returns the document ids retrieval may return, or None. None is NOT a
    # neutral default: it means no governance is being enforced and every
    # document in the index can reach a patient. search_candidates records that
    # as eligibility_enforced=False rather than leaving it to be inferred from
    # an absent argument, and the reason is logged here, because the failure
    # this guards against is a pack quietly ceasing to constrain anything.
    def _eligible_documents():
        """(ids or None, reason). Never raises."""
        try:
            from datetime import date as _date

            from synapse.governance.eligibility import select_eligible
            from synapse.governance.pack import SourcePack

            _pack = SourcePack.load(
                Path(os.getenv("SYNAPSE_SOURCE_PACK", "source_packs/diabetes-previsit"))
            )
            _eligible, _ = select_eligible(_pack.sources, _pack.manifest, as_of=_date.today())
            _ids = frozenset(source.document_id for source in _eligible)
            if not _ids:
                return None, "pack authorises no sources"
            return _ids, f"{len(_ids)} approved source(s)"
        except Exception as exc:
            # An unapproved or unreadable pack authorises nothing. Serving
            # UNFILTERED is the current behaviour and is kept so the app still
            # works, but it is never silent: see the log line below and
            # eligibility_enforced in the retrieval trace.
            return None, f"not enforced ({type(exc).__name__})"

    eligible_documents, eligibility_reason = _eligible_documents()
    _app_logger = __import__("logging").getLogger("synapse.app")
    _app_logger.info(
        "retrieval governance resolved",
        extra={
            "eligibility_enforced": eligible_documents is not None,
            "eligible_documents": len(eligible_documents) if eligible_documents else 0,
            "reason": eligibility_reason,
        },
    )
    built = {}                                           # Anything the worker builds, written back to session state below

    # Conversation context for the RETRIEVAL query only, snapshotted here on the
    # main thread for the same reason chunks and hybrid are: the retrieve closure
    # runs on a worker with no ScriptRunContext, where st.session_state is not
    # readable. The narrowing to two strings per turn is deliberate — no typed
    # presentation object crosses into synapse.memory.
    #
    # Excluded: failed turns (no answer at all) and emergency escalations, which
    # are produced ahead of any retrieval and so carry no evidence context. The
    # summary is read off the validated GroundedAnswer, never re-derived from
    # rendered prose. This reads the conversation list; it never mutates it.
    def _history_for_retrieval(limit=3):
        """The last `limit` eligible turns, oldest first. Never raises."""
        summaries = []
        try:
            for _t in st.session_state.conversation:
                _outcome = _t.get("outcome")
                if _outcome is None or not _outcome.ok:
                    continue
                _answer = _outcome.presentation.answer
                if _answer.action is AnswerAction.EMERGENCY:
                    continue
                # An abstention's summary is fixed boilerplate, substituted by
                # synapse.answer.policy.apply and identical for every abstained
                # turn. It carries no context, and it states that the sources
                # came up empty -- which is a fact about the corpus, not about
                # what the patient is asking, and steers the rewrite wrong.
                #
                # The turn is kept rather than dropped: the patient's own
                # question is the context that resolves a follow-up. After
                # "what is metformin?" abstains, "what about the side effects?"
                # still means metformin's.
                _summary = "" if _answer.action is AnswerAction.ABSTAIN else _answer.summary
                summaries.append(
                    TurnSummary(query=_t.get("query", ""), answer_summary=_summary)
                )
        except Exception:
            # Context is an optimisation. A malformed turn degrades retrieval
            # quality; it must never stop the patient getting an answer.
            return []
        return summaries[-limit:]

    history = _history_for_retrieval()

    # Safety state, kept SEPARATE from the retrieval history above, which
    # deliberately excludes escalations. A red flag latches: a patient who
    # described crushing chest pain and then asked "is that serious?" was
    # escalated once and then answered normally, because the follow-up carries
    # no emergency vocabulary of its own for the detector to fire on.
    #
    # Bounded to the same window as the retrieval history, so the latch releases
    # after that many non-escalated turns rather than ending the session.
    def _recent_escalation(limit=3):
        """True if any of the last `limit` turns escalated. Never raises."""
        try:
            recent = list(st.session_state.conversation)[-limit:]
            return any(
                _t.get("outcome") is not None
                and _t["outcome"].ok
                and _t["outcome"].presentation.answer.action is AnswerAction.EMERGENCY
                for _t in recent
            )
        except Exception:
            # Fail CLOSED. Unlike the retrieval history, where a fault costs
            # quality, a fault here costs a red flag -- so an unreadable
            # conversation escalates rather than staying silent.
            return True

    recent_escalation = _recent_escalation()

    def retrieve(user_query):
        """Retrieve a bounded candidate set and rerank it in one request.

        Runs on a worker thread, so it touches neither st.session_state nor any
        Streamlit call — it reports progress by setting `stage["label"]`, which
        the main thread reads.

        The ordering and the limits live in synapse.retrieval, not here. This
        function loads the index, wraps it in the two backend adapters, and
        hands over. It returns a typed RetrievalBundle, so no identifier is
        derived, repaired or dropped anywhere downstream.
        """
        from synapse.answer.providers import OpenAIStructuredClient
        from synapse.memory.query_rewrite import rewrite_query
        from synapse.retrieval import (
            DEFAULT_CANDIDATE_CONFIG,
            DEFAULT_RERANK_CONFIG,
            bundle_from_candidates,
            rerank_candidates,
            search_candidates,
        )
        from synapse.retrieval.production import LegacyDenseBackend, LegacySparseBackend

        chunks = cached_chunks
        hybrid = cached_hybrid

        if not chunks:
            stage["label"] = "Loading the medical research index..."
            from Data.fetch_and_chunk import load_chunks
            chunks = load_chunks("processed_chunks.pkl")
            built["chunks"] = chunks

        if hybrid is None:
            stage["label"] = "Checking the research index..."
            # Fail closed BEFORE any retrieval: an index whose contents do not
            # match its manifest cannot be served from, because a citation would
            # then point at a different document than the one that was read. The
            # exception propagates to answer_turn, which renders a failure card
            # and generates nothing at all.
            from synapse.retrieval.index_gate import check_index
            built["index_gate"] = check_index(Path("hybrid_index"))

            stage["label"] = "Opening the research index..."
            from Retrieval.hybrid_retriever import HybridRetriever
            try:
                hybrid = HybridRetriever.load("hybrid_index", fusion="linear", alpha=0.7)
            except (OSError, ValueError, RuntimeError):
                # No prebuilt index on disk, or an unreadable one. RuntimeError
                # is included because that is what FAISS raises for a missing
                # index file. Anything else propagates to answer_turn, which
                # turns it into a typed retrieval_failed outcome.
                stage["label"] = "Preparing the medical research index..."
                hybrid = HybridRetriever(fusion="linear", alpha=0.7)
                hybrid.build(chunks, api_key=api_key)
            built["hybrid"] = hybrid

        # Resolve a context-dependent follow-up ("what about the side effects?")
        # into a standalone query, so BM25 and FAISS see the whole question
        # rather than six context-free words.
        #
        # RETRIEVAL ONLY. The rewritten string never leaves this closure:
        # answer_turn passes its own `query` to generation, so the patient's raw
        # words are what reach build_user_prompt and what every claim is verified
        # against. Nothing about the generation contract changes, which is why
        # generate.PROMPT_ID does not move.
        #
        # Fails open on every path, like rerank below: was_rewritten False means
        # retrieval_query IS user_query and behaviour is identical to before.
        # The model and timeout are the rerank config's, not the answer model's:
        # this is a cheap bounded rewrite on the patient's critical path.
        stage["label"] = "Understanding your question..."
        try:
            retrieval_query, was_rewritten = rewrite_query(
                user_query,
                history,
                OpenAIStructuredClient(
                    api_key=api_key,
                    model=DEFAULT_RERANK_CONFIG.model,
                    timeout=DEFAULT_RERANK_CONFIG.read_timeout,
                ),
            )
        except Exception:
            # Belt and braces. rewrite_query catches everything internally and is
            # tested for it, so this should be unreachable — but it runs inside
            # the closure answer_turn wraps in its own retrieval try/except, and
            # there an escaping exception becomes a retrieval_failed card. That
            # would cost the patient the whole turn to save them a query rewrite.
            # Degrade to the raw query instead, which is the pre-rewrite
            # behaviour and always a valid thing to search for.
            retrieval_query, was_rewritten = user_query, False

        stage["label"] = "Searching relevant research..."
        # Bounded on both sides. Neither count is derived from the corpus size:
        # the previous implementation asked FAISS for top_k=len(chunks) and BM25
        # for a score per chunk, on every query.
        retrieval = search_candidates(
            retrieval_query,
            dense=LegacyDenseBackend(hybrid.vector_store, api_key),
            sparse=LegacySparseBackend(hybrid.bm25_index),
            config=DEFAULT_CANDIDATE_CONFIG,
            eligible_documents=eligible_documents,
        )

        stage["label"] = "Ranking the strongest evidence..."
        # ONE request for the whole candidate set, with its own timeouts,
        # deadline and bounded retries. On failure this returns the fused
        # retrieval order unchanged rather than raising: a ranking that could
        # not be improved is still a usable ranking, and every claim built on it
        # is still verified by the answer layer.
        outcome = rerank_candidates(
            retrieval_query,
            retrieval.candidates,
            OpenAIStructuredClient(
                api_key=api_key,
                model=DEFAULT_RERANK_CONFIG.model,
                timeout=DEFAULT_RERANK_CONFIG.read_timeout,
            ),
            DEFAULT_RERANK_CONFIG,
        )

        stage["label"] = "Preparing your answer..."
        # A BOOLEAN, and nothing else. Neither the original nor the rewritten
        # query, nor any substring of either, is recorded anywhere: a rewritten
        # query is still the patient's question (app.py header, and
        # docs/privacy-logging-policy.md). Carried in the bundle metadata because
        # provenance is built before answer_turn runs, when this is not yet
        # known; pipeline.py emits every non-dict metadata value as
        # retrieval_query_rewritten.
        # The resolved query is nested inside a dict ON PURPOSE. answer_turn's
        # "turn rendered" log line spreads every NON-dict metadata value into
        # its extra={}, so a bare string here would put the patient's question
        # into the logs -- the one thing the telemetry policy forbids. Nested,
        # it is skipped by that comprehension and ignored by
        # _record_retrieval_shape, while still reaching the renderer through
        # AnswerPresentation.conversion. The boolean stays flat, because a
        # boolean is exactly what telemetry is allowed to keep.
        return bundle_from_candidates(
            outcome.candidates,
            metadata={
                "retrieval": retrieval.trace.as_metadata(),
                "rerank": outcome.as_metadata(),
                "query_rewritten": was_rewritten,
                # Flat so it reaches the "turn rendered" log line; the nested
                # retrieval trace carries the same fact as eligibility_enforced.
                "governed": eligible_documents is not None,
                "rewrite": {"resolved_query": retrieval_query if was_rewritten else ""},
            },
        )

    # One call. Every failure inside — a detector fault, a retrieval error, an
    # unreachable provider, a malformed generation — comes back as a typed
    # failure rather than an exception or, worse, unvalidated prose.
    outcome = run_with_progress(
        loading,
        lambda: answer_turn(
            query,
            retrieve=retrieve,
            client=OpenAIStructuredClient(api_key=api_key),
            is_emergency=load_detector().is_emergency,
            # Prior patient QUESTIONS only, off the same main-thread snapshot the
            # retrieval rewrite uses. Not the prior answers: an answer summary is
            # not in the passages, so a model quoting one would cite evidence
            # that was never retrieved and lose the claim.
            #
            # This does not reach is_emergency, which still sees the raw query
            # alone, and it does not reach retrieve.
            history=[_t.query for _t in history],
            recent_escalation=recent_escalation,
            telemetry=TELEMETRY,
            provenance={
                "provider": "openai",
                "model": DEFAULT_MODEL,
                "prompt_version": SYNAPSE_PROMPT_VERSION,
                "rerank_prompt_version": RERANK_PROMPT_ID,
                "corpus_version": st.session_state.get("corpus_version", ""),
                "index_version": st.session_state.get("index_version", ""),
            },
        ),
        0.10,
        0.96,
        lambda: stage["label"],
    )

    # Session state is written on the main thread only.
    if "chunks" in built:
        st.session_state.chunks = built["chunks"]
        st.session_state.chunks_built = True
    if "hybrid" in built:
        st.session_state.hybrid = built["hybrid"]

    # Announce the outcome once, politely, in plain language (requirement 23).
    if outcome.ok:
        loading.progress(100, text="Answer ready")
        announcement = "Your answer is ready below."
    else:
        loading.progress(100, text="No answer available")
        announcement = (
            "Synapse could not prepare an answer. There is an explanation below."
        )
    status_slot.markdown(
        f'<div role="status" aria-live="polite" class="visually-hidden">{escape(announcement)}</div>',
        unsafe_allow_html=True,
    )

    st.session_state.conversation.append({"query": query, "outcome": outcome})
    st.rerun()
