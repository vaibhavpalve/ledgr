# ADR-094: Invoice reading switches to Mistral AI; Vertex/Claude blocked by GCP billing tier

- **Status**: Accepted
- **Date**: 2026-09-27
- **Serves**: FR-EXP-001c, FR-AP-002 (same as ADR-081)
- **Constrained by**: PRIV-010, PRIV-011, PRIV-016, CLAUDE.md non-negotiable 4 (integrations sit
  behind adapters)
- **Builds on**: [ADR-081](ADR-081-automatic-invoice-reading.md) (built the feature and its
  `InvoiceExtractor` port), [ADR-093](ADR-093-vertex-ai-retained-over-mistral-for-invoice-reading.md)
  (evaluated Mistral against Vertex, deferred it)

## Context

ADR-093 decided to enable Vertex now and keep Mistral as a documented, cheap-to-add second adapter,
revisited only on a concrete trigger rather than speculatively built. Working through ADR-081's
"Before it is enabled" checklist surfaced exactly such a trigger: Google's Model Garden could not
grant this project's account access to the Claude model at all. The project's GCP billing account
is on a free/trial tier, and Anthropic's partner models on Vertex require an upgraded (non-trial)
billing account to request access. This is not a capability, region, or compliance problem — the
Vertex AI API enabled cleanly, the IAM role granted cleanly, and `europe-west1` did offer the model
— it is a billing-account-tier gate in front of the one remaining step, with no confirmed timeline
to clear.

## Decision

**Build and deploy `MistralExtractor` now, rather than resolve the GCP billing blocker.**

- **The port already existed for exactly this.** ADR-081's `InvoiceExtractor` protocol and
  ADR-093's own framing anticipated a second adapter being cheap; implementing it was one new file
  (`api.expenses.extraction.mistral`) and a branch in `build_extractor`, not a rewrite.
- **Mistral's La Plateforme needs only an API key from a Mistral account** — no GCP billing-account
  upgrade, no partner-model approval flow gated on billing tier. It unblocks the feature without
  waiting on an unconfirmed upgrade process.
- **ADR-093's cost/benefit reasoning about Mistral vs. Vertex is otherwise unchanged.** This still
  adds a new sub-processor, a new DPA, and a PRIV-016 entry — real cost, same as ADR-093 weighed —
  but it is now the only reachable option today, not a speculative alternative being built ahead of
  need.
- **Same checked-reading contract as Vertex.** `MistralExtractor` sends one request per invoice (a
  PDF as `document_url`, a JPEG/PNG as `image_url`, both as base64 data URIs), forces the answer
  through the same single `record_invoice` tool (`tool_choice: "required"` with only that one tool
  offered), and the response goes through the same `parse_reading` every other provider's answer
  goes through — checked, range-checked, never repaired. Only the transport and the provider name
  differ from `VertexClaudeExtractor`.

## Consequences

- `api.config.Settings.extraction_provider` gains `"mistral"`; `extraction_mistral_api_key` is
  added with no default — selecting `mistral` without a key fails at build time with a plain
  sentence (`ExtractionNotConfigured`), the same posture `vertex-claude` without a project already
  has, rather than failing on the first captured invoice.
- **PRIV-016**: Mistral is a new sub-processor. The 30 days' notice isn't a blocker today — still
  Datapal BV's own test/demo tenant, per ADR-081's compliance note — but must be done before this
  reads a real customer's invoice.
- `VertexClaudeExtractor` and its Google service-account plumbing (`google_auth.py`) are left in
  place, untouched, and still selectable via `EXTRACTION_PROVIDER=vertex-claude`. If the GCP
  billing account is later upgraded and Model Garden access granted, switching back is a settings
  change, not un-deleting code.
- **A process note worth keeping for future ADRs shaped like ADR-093's**: the "revisit trigger, not
  a timer" framing fired almost immediately, and on an *operational* ground (a billing-tier gate)
  rather than a customer-driven one. An unverified assumption underneath a decision ("Model Garden
  access is obtainable here") is itself worth checking before committing to a plan around it, not
  only watched for after the fact.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Upgrade the GCP project's billing account to unblock Vertex | A real option — adding a payment method may be all it takes — but with no confirmed approval timeline for Model Garden access even after upgrading, against Mistral needing only a signup. Worth revisiting if Mistral turns out insufficient for extraction quality. |
| Wait until Vertex access resolves, ship nothing | Rejected: invoices arriving empty and needing manual entry is the same validated pain point ADR-081 exists to fix, and the alternative was already fully evaluated in ADR-093 — no new analysis needed to act on it now. |
