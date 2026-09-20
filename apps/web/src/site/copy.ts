/**
 * The marketing site's copy, verbatim from design/reference/Website.html.
 *
 * Three lines differ from the reference on purpose, because the product cannot back them:
 * "Start free trial" (there is no trial), "Bank files import and match automatically" (no bank
 * import exists; the tick is dropped) and the mockup's "Due in 42 days" (see Website.tsx). Everything else is verbatim.
 *
 * English only: the reference has no Dutch, and inventing a translation of
 * marketing claims is a decision for the owner (FR-LOC-001 asks for both). Kept
 * out of the component so the words can move to the message catalogue in one
 * step once there is Dutch to put beside them.
 */
export const COPY = {
  nav: { product: "Product", books: "The books", accountants: "For accountants" },
  signIn: "Sign in",
  cta: "Create account",
  hero: {
    badge: "Dutch bookkeeping for small businesses and accountants",
    lead: "From receipt to return,",
    mark: "automatically.",
    body: "Capture receipts on your phone, keep a permanent grootboek and see your BTW at any time. Built for how Dutch small businesses and their accountants actually work.",
    secondary: "See how it works",
  },
  facts: [
    ["Exact to the cent", "Decimal arithmetic, never floating point."],
    ["Permanent entries", "Corrections are new entries, never deletions."],
    ["Works offline", "Capture receipts with no signal."],
    ["Passkey sign-in", "One step, no password to remember."],
  ],
  capture: {
    eyebrow: "CAPTURE",
    title: "Snap a photo. We book it.",
    lead: "Photograph a receipt from your phone, even with no signal. Ledgr reads the supplier, date, total and BTW, proposes the entry and books it the moment you are back online.",
    ticks: [
      "Works offline, syncs when you reconnect.",
      "Suggests the account and BTW rate for you to confirm.",
      "Every receipt stays attached to its entry.",
    ],
  },
  books: {
    eyebrow: "THE BOOKS",
    title: "A grootboek that never changes quietly.",
    lead: "Every entry is permanent. A correction is a new entry that everyone can see, never a deletion. Amounts are calculated in exact decimals, down to the cent.",
    ticks: [
      "Double-entry, enforced on every booking.",
      "Dutch chart of accounts and BTW built in.",
    ],
    balanced: "Balanced to the cent. Correct it later with a new entry.",
  },
  accountants: {
    eyebrow: "FOR ACCOUNTANTS",
    title: "Every client in its own colour.",
    lead: "Switch between clients deliberately. Each one has its own colour, name and initials on screen at all times, so nothing is ever posted to the wrong books.",
    ticks: [
      "See what needs attention across every client.",
      "Review captured receipts before they count.",
    ],
    heading: "YOUR CLIENTS",
  },
  band: "Close the books without the chase.",
  footer: {
    blurb: "Dutch bookkeeping for small businesses and their accountants.",
    product: ["Capture", "Invoices", "Grootboek", "BTW"],
    company: ["About", "Contact", "Privacy", "Terms"],
  },
} as const;
