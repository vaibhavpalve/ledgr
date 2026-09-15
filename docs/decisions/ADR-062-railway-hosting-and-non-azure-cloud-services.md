# ADR-062: Hosting moves to Railway; Cloudflare R2 and Google Cloud KMS replace Azure Blob and Key Vault

- **Status**: Accepted
- **Date**: 2026-09-15
- **Amends**: [ADR-001](ADR-001-stack-selection.md) (Cloud, Document storage, Secrets rows),
  [ADR-004](ADR-004-envelope-encryption.md) (production KMS), [ADR-030](ADR-030-document-storage.md)
  (production `BlobStore`), [ADR-020](ADR-020-audit-log.md) (audit-chain anchor sink target)

## Context

ADR-001 named Azure/West Europe as the cloud, citing PRIV-010 (EU data residency) as the reason.
At pre-revenue, pre-funding stage, Azure's PaaS pricing (App Service + managed PostgreSQL Flexible
Server + Key Vault) runs materially higher than alternatives that satisfy the same requirement —
EU residency does not require Azure specifically, only that data and the services touching it stay
in the EU. This ADR picks a cheaper combination that still satisfies every requirement Azure was
chosen to serve, per CLAUDE.md's own rule that "any further deviation must still satisfy the
requirement each choice was made to serve."

Three things Azure was providing have to be replaced, not merely removed:

1. **Compute + PostgreSQL** (App Service, Azure Database for PostgreSQL) — SEC-022/IAM-002 need
   nothing Azure-specific here; the schema is plain PostgreSQL with row-level security, `pgcrypto`
   (`migrations/0001_tenancy_core.sql:24`) and `pg_trgm` (`migrations/0018_client_switcher.sql:41`
   and others), all standard contrib extensions.
2. **A managed KMS for envelope encryption** (SEC-022: "envelope encryption with a managed
   KMS/HSM"; SEC-023: annual rotation, tested emergency rotation) — `api.crypto.kms.
   KeyManagementService` (`current_kek_id`, `wrap_key`, `unwrap_key`) is the Protocol; the only
   production implementation written against it is `AzureKeyVaultKeyManagementService`
   (`apps/api/src/api/crypto/kms.py:117-157`), which per ADR-004's own Consequences section "could
   not be exercised against a live Key Vault in this environment" — there was no working, tested
   Azure integration to lose.
3. **Object storage for documents** (FR-DOC-001/005, IAM-004: per-tenant keys, write-once for the
   retention period) — `api.documents.storage.BlobStore` (`put`/`get`/`delete` only, no `list`/
   `url_for` per SEC-005) is the Protocol; **no Azure Blob implementation exists at all**.
   `InMemoryBlobStore` is wired in `api.documents.routes` today and named, in ADR-030's own words,
   as a stand-in ("the posture `api.crypto.kms` ... take[s] for their own local stand-ins"). Nothing
   real is being replaced here either — only the *planned* production target changes.

In both (2) and (3), this decision has zero migration cost in code terms: the adapters being
"replaced" were never built or never verified. It is a change to what gets built next, not a
rollback of working infrastructure.

## Decision

### Compute and database: Railway, pinned to its EU region

Railway (railway.com) hosts the FastAPI service and a managed PostgreSQL instance, both explicitly
pinned to Railway's **Amsterdam (europe-west4)** region — not left at Railway's default, since
PRIV-010 requires the data actually stay in the EU, not merely "be placeable there." This must be
set per-service at provisioning time and re-confirmed on any redeploy; Railway does not guarantee
EU residency unless the region is explicit.

PostgreSQL itself is unaffected: no migration in this repo uses an Azure-specific feature (Azure AD
database auth, a managed-identity connection string) — every migration runs against
`postgresql+asyncpg://` with standard extensions. Before the first migration runs against Railway's
Postgres for real, `CREATE EXTENSION pgcrypto` and `CREATE EXTENSION pg_trgm` must be confirmed
available on Railway's Postgres image — treated as unverified until checked, not assumed, matching
this project's own standing rule (no migration ships as "tested" until it has run against a real
target Postgres).

### Object storage: Cloudflare R2, with the EU jurisdictional restriction enabled

R2's bucket-level "EU jurisdictional restriction" is Cloudflare's PRIV-010-equivalent control: it
constrains both data *and metadata* to EU infrastructure and EU sub-processors, the same property
Azure Blob's West Europe region was chosen for. R2 speaks the S3 API, so the concrete work this ADR
requires (not done here) is one new `BlobStore` implementation — an S3-compatible client
implementing the same three methods `EncryptedBlobStore` already wraps
(`apps/api/src/api/documents/storage.py:76-158`) — not a change to the Protocol or to
`EncryptedBlobStore` itself, which is provider-agnostic by construction.

R2 charges no egress fees, which matters specifically for this product: every time a client
re-opens a stored receipt or invoice, or an accountant reviews a client's archive, that is an
egress read against a 7-year-retained document store (FR-DOC-002) — a cost that compounds with
usage on providers that charge for it and does not on R2.

FR-DOC-005's write-once guarantee is unaffected either way: it is enforced today at the database
layer (`documents.delete_with_approval`, granted only to `ledgr_ops`; `ledgr_app` has no `DELETE`
grant at all — ADR-030 §5), not by a storage-provider immutability policy that was never actually
configured for Azure Blob. R2 also supports S3-compatible Object Lock, should a provider-side
control be added later; this ADR does not require it, since ADR-030's guarantee never depended on
one existing.

### Envelope encryption: Google Cloud KMS, same region as Railway's compute

Google Cloud KMS in `europe-west4` (Netherlands) replaces Azure Key Vault as the KEK holder in the
three-layer hierarchy ADR-004 already defined (KMS-held KEK → per-administration DEK → AES-256-GCM
document encryption) — that hierarchy does not change, only which service holds the KEK and answers
`wrap`/`unwrap` calls. Chosen over AWS KMS (an equally valid alternative — see below) specifically
because Railway's own infrastructure runs on GCP, so KEK wrap/unwrap calls stay within one cloud
rather than adding a third vendor to the two this ADR already introduces (Railway, Cloudflare).

The concrete work this ADR requires (not done here): a `GcpKmsKeyManagementService` implementing
the same `KeyManagementService` Protocol `AzureKeyVaultKeyManagementService` does
(`apps/api/src/api/crypto/kms.py:45-52`), and a new `kms_provider` literal value (today:
`Literal["local", "azure-key-vault"]`, `config.py:17`) to select it. `LocalDevKeyManagementService`
is untouched — it already exists for exactly this kind of provider swap and remains the non-
production default.

### The audit-chain anchor's target names R2/GCS instead of Azure Blob

ADR-020 named "Azure Blob under a WORM policy in a separate subscription" as the intended, not-yet-
built sink for the audit-chain head anchor (`app.audit_chain_head()`). That target updates to
"object storage under Object Lock, in a separate Cloudflare/GCP account from the one holding
document blobs" — still unbuilt, still named as remaining work rather than implied done, exactly as
ADR-020 already stated it. Nothing here changes the urgency or design of that follow-up, only which
provider it would target.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Stay on Azure, offset cost with Microsoft for Startups Founders Hub credit | Credit is temporary and the underlying PaaS markup returns once it lapses; Railway's simpler deploy model (git push, no IaC to author for an MVP) also reduces engineering time cost, not just cloud spend, which matters more at this stage than which line the savings show up on. |
| Hetzner or another unmanaged EU VPS, with a self-hosted Vault/HSM for key management | Would put this project in direct tension with SEC-022's literal wording — "envelope encryption with a **managed** KMS/HSM" — self-hosting key management is not a managed KMS, and building/hardening an HSM-equivalent yourself is a larger, riskier undertaking than paying a few dollars a month for one. Cheaper compute is not worth that regression. |
| AWS (S3 + AWS KMS) instead of Cloudflare R2 + Google Cloud KMS | A fully valid alternative — both are managed, EU-region-capable services. Not chosen because (a) S3 charges egress and R2 does not, which compounds for a document-archive product with repeat reads, and (b) Railway's infrastructure is GCP-backed, so GCP KMS keeps the KMS calls within one cloud rather than introducing AWS as a third vendor alongside Railway and Cloudflare. Revisit if Railway's own infrastructure choice changes. |
| Move compute to Railway but keep Azure Blob and Key Vault | Rejected: keeping any Azure service preserves the Azure billing relationship and subscription overhead this decision exists to remove, while adding cross-cloud network hops between Railway (GCP) and Azure for every document read/write and every key operation — the worst combination of "still paying Azure" and "now also paying the latency cost of splitting across clouds." |

## Consequences

- **ADR-001's stack table** (Cloud, Document storage, Secrets rows) is superseded by this ADR for
  those three rows only; every other row (FastAPI, Postgres+RLS, `pg_trgm`, React/React Native)
  is unaffected and still stands.
- **Not built by this ADR — concrete follow-up work**, each with its own trigger before this
  decision is real rather than documented:
  - A `GcpKmsKeyManagementService` (mirrors `AzureKeyVaultKeyManagementService`'s shape) and the
    `kms_provider` literal/settings to select it.
  - An S3-compatible `BlobStore` implementation for R2, wired into `api.documents.routes` in place
    of `InMemoryBlobStore` for non-local environments.
  - `pgcrypto`/`pg_trgm` availability confirmed against Railway's actual Postgres image, before the
    first real migration run there.
  - `AzureKeyVaultKeyManagementService` should be deleted once the GCP implementation lands and is
    selected in every non-local environment — not left in-tree as a second, silently-unused
    "supported" option (CLAUDE.md: no backwards-compatibility shims for code known unused).
- **A new PRIV-010/011 sub-processor review is required** against the actual new vendor list —
  Railway, its underlying infrastructure provider, Cloudflare, and Google Cloud — rather than
  assuming the diligence already done for an Azure-only chain carries over. This must happen before
  real customer data reaches any of these services, not after.
- Local development's blob stand-in can stay `InMemoryBlobStore` as-is; the `.env` file's Azurite
  setup (installed for a Blob emulator that nothing in the code ever consumed) becomes unnecessary
  cruft to remove in the same change that adds the real R2 adapter, not before.
- `CLAUDE.md`'s "Confirmed stack" section still names Azure/West Europe and will read as stale
  until it is updated to record this as a third deliberate deviation, alongside the two it already
  lists (FastAPI-only, Postgres full-text search) — a small, separate edit, not done as part of
  writing this ADR.
