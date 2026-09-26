# ADR-093: Invoice reading launches on Vertex AI; Mistral AI evaluated and deferred, not rejected

- **Status**: Accepted
- **Date**: 2026-09-27
- **Serves**: FR-EXP-001c, FR-AP-002 (same as ADR-081)
- **Constrained by**: PRIV-010, PRIV-011, PRIV-016, CLAUDE.md non-negotiable 4 (integrations sit
  behind adapters)
- **Builds on**: [ADR-081](ADR-081-automatic-invoice-reading.md) (built the feature, off by
  default), [ADR-062](ADR-062-railway-hosting-and-non-azure-cloud-services.md) (the Google Cloud
  service account this reuses)

## Context

ADR-081 built automatic invoice reading on Claude served from Google Vertex AI, off by default
pending the GCP-side setup. Before flipping it on, it's worth asking whether Vertex is still the
right choice given this product is built for Dutch SMBs and the accounting firms that serve
them — the Netherlands (and the EU generally) has a live digital-sovereignty conversation, and the
obvious EU-native alternative is Mistral AI: a French company with no US parent, offering both a
hosted API and a purpose-built document/OCR extraction product.

The distinction that matters is not data *residency* — Vertex in `europe-west4` already satisfies
PRIV-010/011, which require data to stay in the EU. It is data *reachability*: Google and Anthropic
are both US companies, so a US legal process (the CLOUD Act) can in principle reach data they
process even when it physically sits in an EU region. Mistral, with no US parent, sits outside that
specific reach. That is a real difference, but it is a deeper sovereignty question than what
PRIV-010/011 literally ask for, and no customer or accounting firm has raised it yet — LEDGR is
still pre-revenue, with only its own test/demo tenant.

## Decision

**Enable Vertex AI now, as ADR-081 already specifies. Do not build a Mistral adapter speculatively.**

- **Reuses an existing trust boundary.** Vertex authenticates with the same Google Cloud service
  account ADR-062 already established for Cloud KMS. Mistral would be a brand-new vendor
  relationship: a new DPA, a new PRIV-016 sub-processor entry, a new secret to manage, a new
  security review — real cost, paid today, for a benefit (CLOUD Act distance) nobody has asked for.
- **The port already isolates this choice.** `api.expenses.extraction.ports.InvoiceExtractor`
  (ADR-081) exists exactly so a provider swap is an adapter, not a rewrite. Adding a
  `MistralExtractor` later costs one implementation and a settings change, not a redesign.
  Non-negotiable 4 is satisfied by keeping the seam ready, not by pre-building both sides of it.
- **Mistral's edge here is legal, not technical.** Mistral's Document AI/OCR product is a credible
  match for structured invoice extraction, and function-calling support means the ADR-081 checked-
  reading pattern (forced tool schema, range-checked, never trusted) would carry over. It is not
  clearly *more* accurate than Claude for this task — the case for switching rests on sovereignty
  positioning, not extraction quality.
- **Hosting Mistral's models on a hyperscaler would give up the one reason to pick it.** Mistral is
  also available through Vertex's and other hyperscalers' model gardens, but routing through Google
  or another US cloud to reach a Mistral model reintroduces the same US-reachable sub-processor this
  ADR is weighing Mistral against. Only Mistral's own EU infrastructure ("La Plateforme") carries the
  sovereignty benefit.

## Consequences

- No new sub-processor, DPA, or secret is added by this decision. The remaining work to turn
  reading on is exactly ADR-081's "Before it is enabled" checklist (Vertex API enabled, Model
  Garden access to the Claude model in `europe-west4`, the service account's IAM role, the two
  Railway variables) — nothing here changes that list.
- **Revisit trigger, not a timer.** Re-open this decision when one of these actually happens, not
  on a schedule: an accounting firm's security/procurement review explicitly raises US CLOUD Act
  reachability; a customer requires an attested EU-only vendor chain; or Vertex/Claude access in
  `europe-west4` turns out unavailable or is withdrawn. Until then, treat
  `api.expenses.extraction.ports.InvoiceExtractor` having exactly one implementation as correct,
  not as a gap.
- If Mistral is added later, it changes nothing in `api.expenses` outside the adapter and the
  provider name in settings (`build_extractor`, ADR-081) — the checked-reading logic
  (`extraction.model.parse_reading`), storage shape, and confidence-flagging UI are provider-agnostic
  already.

## Alternatives considered

| Alternative | Why not (now) |
|---|---|
| Mistral AI, own infrastructure (La Plateforme) | Strongest EU-sovereignty story of the options, and has a purpose-built document-extraction product — but is a new vendor relationship (DPA, sub-processor entry, secret) paid for today against a benefit no customer has asked for yet. Worth building the day that changes. |
| Mistral models hosted via a hyperscaler's model garden (Vertex, Bedrock, Azure) | Keeps the existing GCP trust chain, but reintroduces a US-reachable sub-processor in front of the model — gives up the one advantage that would justify choosing Mistral over Claude/Vertex in the first place. |
| Anthropic's own API, direct | Already rejected in ADR-081: not EU-resident by default, no documented DPA exception. Unchanged by this ADR. |
| Do nothing (leave reading off) | Rejected: invoices arriving empty and needing manual entry is a validated pain point; ADR-081 is fully built, and the only remaining steps are operational (GCP + Railway configuration), not development. |
