# LEDGR

LEDGR is a multi-tenant cloud bookkeeping platform for Dutch SMBs and the accounting firms that
serve them, automating the path from source document to filed tax return. It ships as a web app,
iOS app, Android app and public API, all running against one API with no client-side business logic.

## Architectural non-negotiables (PRD §13)

1. **The ledger is a separate bounded context with a narrow API.** Nothing writes to posting
   tables except the ledger service.
2. **Authorization is a single library used by every service.** There is no second implementation.
3. **Every request carries tenant context from edge to database.** A request without tenant
   context fails closed.
4. **Integrations (banks, Peppol, Digipoort, PSPs) sit behind adapters**, so a provider can be
   replaced without touching domain logic.

Confirmed stack: Azure/West Europe, **FastAPI (Python)** REST+OpenAPI, PostgreSQL with row-level
security, append-only ledger tables (no UPDATE/DELETE grants on committed rows), Azure Blob with
per-tenant keys, React/TypeScript web, React Native mobile.

Two deviations from the PRD §13 table, confirmed deliberately rather than defaults:
- **API**: FastAPI only, not "FastAPI or .NET" — one language across API and async workers keeps
  the integration-adapter surface (non-negotiable #4) smaller, and the document-AI/OCR pipeline
  is Python-native anyway.
- **Search**: Postgres full-text search (`pg_trgm`) instead of OpenSearch. NFR-008 only requires
  <2s search over a 7-year archive, which Postgres FTS on the already-RLS-partitioned tables
  satisfies without a second tenant-partitioned datastore to secure and operate.

The rest of the PRD §13 table stands as proposed. Any further deviation must still satisfy the
requirement each choice was made to serve.

## Four rules that must never be violated

1. **Every request carries tenant context.** Every data record carries `organization_id` and,
   where applicable, `administration_id`. No query path may return records without a tenant
   predicate. Tenant isolation is enforced at the data layer (row-level security) as the first
   line of defense — application-layer checks are the second line, never the only line. Every
   new endpoint needs an automated tenant-isolation test before it can ship. (IAM-001–005)
2. **The ledger is append-only.** Postings are immutable once committed. Corrections happen via
   reversing entries, never mutation or deletion — including through support tooling. No
   interface, including admin/support tooling, may delete a posted entry. (FR-GL-003, CMP-009)
3. **One authorization library, used everywhere.** All authorization decisions are evaluated
   server-side per request against current state, through the single shared library. Client-side
   hiding of UI is presentation only, never the control. Default deny: absence of a matching
   grant is a denial. (IAM-030–037)
4. **No financial calculation uses floating point.** Monetary values use decimal types with
   defined scale, everywhere in the calculation path — ledger, invoicing, VAT, reporting.
   (NFR-031)

## Where the PRD lives / requirement IDs

The PRD is at [prd.md](prd.md) (repo root). Requirements are cited by ID, not by section number
or paraphrase — e.g. `FR-GL-003`, `IAM-105`, `NFR-031`, `SEC-005`, `PRIV-015`, `CMP-009`,
`MOB-008`. ID prefix meanings:

| Prefix | Domain |
|---|---|
| `FR-<module>-nnn` | Functional requirement |
| `NFR-nnn` | Non-functional requirement |
| `IAM-nnn` | Identity and access management |
| `SEC-nnn` | Security |
| `PRIV-nnn` | Privacy and data protection |
| `CMP-nnn` | Regulatory compliance |
| `MOB-nnn` | Mobile-specific |

When implementing or discussing a requirement, reference its ID directly (e.g. in commit
messages, PR descriptions, code comments where genuinely non-obvious) rather than restating it in
prose. Priority is MoSCoW (M/S/C/W) — a requirement's priority in the PRD governs whether it's
in scope for the current phase, not local judgment.

## Commit convention

Reference the requirement ID(s) a change implements or touches, e.g.:

```
feat(ledger): reversing entries for posted transactions (FR-GL-003)
```

If a change touches tenancy, authorization, or the ledger's append-only guarantee, say so
explicitly in the commit body — these are the properties most likely to regress silently.

## Test expectations

- Every new endpoint ships with an automated tenant-isolation test (IAM-005) — no exceptions.
- The accounting/ledger engine carries property-based tests asserting its core invariants:
  balance (debits = credits), immutability, and period integrity (NFR-042).
- Idempotency keys are required on all mutating API endpoints; retries must never double-post
  (NFR-032).
- Migrations must be backward compatible and reversible, and must not require downtime (NFR-044).
