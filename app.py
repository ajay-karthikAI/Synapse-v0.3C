"""
Synapse
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

# The application service. Everything this file used to do between "the patient
# pressed send" and "there is a typed outcome to render" now lives in
# synapse.service, which imports no interface framework and is exercised offline
# against fakes. app.py supplies a progress reporter and renders the result.
from synapse.service import (
    STAGE_MESSAGES,
    Conversation,
    MissingCredentialError,
    ProgressStage,
    ServiceConfig,
    SynapseService,
)

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
    # `conversation` is a synapse.service.Conversation: ordered ConversationTurn
    # objects carrying a typed synapse.ui.TurnOutcome each, never dicts of free
    # text. Nothing in this file may re-derive structure from prose.
    #
    # The corpus, the index and the API key are NOT here any more. The first two
    # are cached inside the service's index provider, which has exactly the
    # lifetime this session state had; the third is read from the process
    # environment and never from the page.
    if "conversation" not in st.session_state:
        st.session_state.conversation = Conversation()

init()


def _service():
    """The application service for this session, built once.

    Held in session state rather than in ``st.cache_resource`` so the index
    cache keeps the lifetime it had before the extraction -- one session -- and
    the change stays a refactor rather than a quiet behaviour change.

    Raises:
        MissingCredentialError: OPENAI_API_KEY is not set in the environment.
            The caller renders the fixed failure copy with a typed code; it does
            not offer anywhere to type a key, because the key is server-side.
    """
    existing = st.session_state.get("service")
    if existing is not None:
        return existing
    # The index provider is passed in rather than built by the service, because
    # synapse may not import the prototype's Data/ and Retrieval/ trees. This is
    # the seam where the legacy stack is bound; see legacy_index.py.
    from legacy_index import legacy_index_provider

    config = ServiceConfig.from_environment()
    config.require_credentials()
    service = SynapseService.from_config(
        config,
        index=legacy_index_provider(
            api_key=config.api_key,
            chunks_path=config.chunks_path,
            index_dir=config.index_dir,
        ),
        telemetry=TELEMETRY,
    )
    st.session_state.service = service
    return service


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
    # The OpenAI key USED TO BE typed here, into a password field that stored it
    # in session state. It is now read from the server's environment only.
    #
    # The field was removed rather than hidden. A credential entered into a page
    # is held in the server process for the life of the session, is unscoped and
    # unrotated, and in any deployment with more than one user would put each
    # person's key into a shared process with no isolation between them
    # (docs/PRIVACY_DATA_FLOW.md §3). Reading it from the environment makes the
    # operator, not the patient, the only party who can supply it.
    st.caption(
        "Synapse uses a key configured by whoever runs it. There is nothing to "
        "enter here."
    )
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
    st.markdown(f'<div class="bubble-user">{escape(turn.query)}</div>', unsafe_allow_html=True)

    outcome = turn.outcome
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
    """The appointment brief: review, edit, choose add-ons, then export.

    ONE brief for the conversation, reached from any answered turn, matching
    `synapse/api/routes/brief.py`. It was one per turn here too, which meant a
    patient who asked four questions was offered four documents.

    Nothing is written to disk or sent anywhere. Every export is an explicit
    click that hands bytes to the browser's download, and the privacy warning is
    shown before the buttons rather than after.
    """
    from synapse.brief import (
        DEFAULT_SECTIONS,
        EXPORT_WARNING,
        RESEARCH_SECTIONS,
        TRANSCRIPT_SECTIONS,
        BriefSection,
        ExportError,
        UserContent,
        add_question,
        build_conversation_brief,
        build_export,
        build_pdf_export,
        estimate_fit,
        overflow_advice,
        remove_question,
        reorder_questions,
        set_notes,
        set_sections,
        set_topic,
    )

    # One key for the whole conversation, not one per turn. The companion key
    # records how many turns the cached brief covers, so a new question widens
    # the document instead of leaving a stale one behind.
    state_key = "brief"
    covered_key = "brief-turn-count"
    turn_count = len(st.session_state.conversation.turns)

    with st.expander("Appointment brief", expanded=False):
        # Built on first open and rebuilt only when the conversation grows.
        # Rebuilding on every interaction would issue a new document identifier
        # each time, and the identifier is what a patient and a clinician use to
        # refer to the same sheet of paper.
        existing = st.session_state.get(state_key)
        if existing is None or st.session_state.get(covered_key) != turn_count:
            # The artifact stamps come off the service config, which is the same
            # place telemetry reads its provenance from. They used to be read
            # from two session-state keys nothing ever set, so a brief and a
            # telemetry record could in principle have disagreed about which
            # corpus produced them.
            _svc = st.session_state.get("service")
            rebuilt = build_conversation_brief(
                st.session_state.conversation,
                resolver=RESOLVER,
                # Carried over so a rebuild does not discard the patient's own
                # topic, notes, questions and choices. Seeded from the first
                # question only when there is nothing to carry.
                user=existing.user
                if existing is not None
                else UserContent(topic=turn.query.strip()[:200]),
                document_id=existing.document_id if existing is not None else None,
                app_version="0.1.0",
                corpus_version=_svc.config.corpus_version if _svc else "",
                index_version=_svc.config.index_version if _svc else "",
            )
            if existing is not None:
                own = [
                    question
                    for question in existing.questions
                    if question.origin.value == "user_authored"
                ]
                rebuilt = rebuilt.model_copy(
                    update={
                        "questions": [*rebuilt.questions, *own][:12],
                        "included_sections": list(existing.included_sections),
                    }
                )
            st.session_state[state_key] = rebuilt
            st.session_state[covered_key] = turn_count
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

        # Two add-ons rather than a list of all eight sections. The multiselect
        # offered "claims", "sources" and "limitations" as separate decisions,
        # which is a decision a patient has no basis for making and the reason
        # the brief read as an engineering document.
        st.markdown("**Add to the PDF**")
        want_transcript = st.checkbox(
            "Your conversation",
            value=BriefSection.TRANSCRIPT in brief.included_sections,
            key=f"{state_key}-addon-transcript",
            help="Every question you asked in this session, and the answer given.",
        )
        want_research = st.checkbox(
            "The research behind it",
            value=all(section in brief.included_sections for section in RESEARCH_SECTIONS),
            key=f"{state_key}-addon-research",
            help="The findings, their citation numbers, and the studies they came from.",
        )
        sections = list(DEFAULT_SECTIONS)
        if want_transcript:
            sections.extend(TRANSCRIPT_SECTIONS)
        if want_research:
            sections.extend(RESEARCH_SECTIONS)
        brief = set_sections(brief, sections)
        st.caption("The summary, your questions and the disclaimer are always included.")
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

        # The PDF is rendered by this process, not by the browser's print
        # dialog. If the renderer is missing -- the streamlit extra can be
        # installed without it -- say so and keep the other three working,
        # rather than failing the whole panel.
        try:
            pdf = build_pdf_export(brief)
        except ExportError:
            pdf = None
        if pdf is not None:
            st.download_button(
                "Download the PDF",
                data=pdf.pdf,
                file_name=pdf.filename,
                mime="application/pdf",
                key=f"{state_key}-pdf",
                type="primary",
                help=f"The brief as a PDF ({max(1, pdf.size // 1024)} KB).",
            )
        else:
            st.caption(
                "The PDF renderer is not installed in this environment. "
                "The other formats below are unaffected."
            )

        bundle = build_export(brief)
        sizes = bundle.sizes()
        left, middle, right = st.columns(3)
        with left:
            st.download_button(
                "Web page",
                data=bundle.html,
                file_name=bundle.html_filename,
                mime="text/html",
                key=f"{state_key}-html",
                help=f"Open and print in a browser ({sizes['html'] // 1024} KB).",
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

if st.session_state.conversation.turns:
    st.markdown(
        '<h2 class="visually-hidden">Your questions and answers</h2>', unsafe_allow_html=True
    )
    for _index, _turn in enumerate(st.session_state.conversation.turns):
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
    loading = st.progress(5, text=STAGE_MESSAGES[ProgressStage.CHECKING_QUESTION])

    from synapse.answer.render import escape, render_failure_html
    from synapse.ui.errors import PATIENT_ERROR_MESSAGE

    # Built on the main thread, before anything reaches a worker.
    try:
        service = _service()
    except MissingCredentialError:
        # A deployment fault, rendered the way every other failure is: fixed
        # application copy and a typed code. It deliberately does NOT say "add
        # your API key" -- there is nowhere for a patient to add one, and
        # telling them to would be an invitation to paste a credential into a
        # page.
        loading.empty()
        status_slot.empty()
        st.markdown(
            render_failure_html(PATIENT_ERROR_MESSAGE, "configuration_error"),
            unsafe_allow_html=True,
        )
        st.stop()

    # Read here, on the main thread. The worker mutates this object; it never
    # touches st.session_state, where there is no ScriptRunContext to read from.
    conversation = st.session_state.conversation

    # The progress reporter. It receives a ProgressEvent carrying a closed
    # ProgressStage and reads the wording off synapse.service's fixed table:
    # there is no parameter through which the query or the answer could reach a
    # progress update, which is the property that makes it safe to display.
    #
    # It runs on the worker thread and only writes this dict. Every
    # progress_bar call happens on the main thread, inside run_with_progress,
    # which reads it.
    stage = {"label": STAGE_MESSAGES[ProgressStage.CHECKING_QUESTION]}

    def report(event):
        stage["label"] = event.message

    # One call. Every failure inside -- a detector fault, a retrieval error, an
    # unreachable provider, a malformed generation -- comes back as a typed
    # failure on the turn rather than as an exception or, worse, as unvalidated
    # prose. The ordering that carries the safety properties lives in
    # synapse.service and synapse.ui.pipeline, not here.
    #
    # The service appends the turn to the conversation itself, because the
    # emergency latch reads that list: a caller that forgot to record a turn
    # would silently release a latched red flag.
    turn = run_with_progress(
        loading,
        lambda: service.ask(query, conversation, report=report),
        0.10,
        0.96,
        lambda: stage["label"],
    )

    # Announce the outcome once, politely, in plain language (requirement 23).
    if turn.answered:
        loading.progress(100, text=STAGE_MESSAGES[ProgressStage.ANSWER_READY])
        announcement = "Your answer is ready below."
    else:
        loading.progress(100, text=STAGE_MESSAGES[ProgressStage.NO_ANSWER])
        announcement = (
            "Synapse could not prepare an answer. There is an explanation below."
        )
    status_slot.markdown(
        f'<div role="status" aria-live="polite" class="visually-hidden">{escape(announcement)}</div>',
        unsafe_allow_html=True,
    )

    st.rerun()
