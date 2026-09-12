# ADR-001: Stack selection

- **Status**: Accepted
- **Date**: 2026-08-19

## Context

PRD §13 ("Architecture and technology") proposes a stack per layer, framed explicitly as
"proposed, not prescribed — but each choice below carries a reason tied to a requirement above."
Before scaffolding the repo, each layer was reviewed against its cited requirement, with one
alternative considered per layer, to confirm or deviate deliberately rather than default to the
table as written.

## Decision

Adopt the PRD §13 stack as proposed, with two deliberate deviations:

| Layer | Choice | Requirement |
|---|---|---|
| Cloud | Azure, West Europe primary, EU failover | PRIV-010 (EU residency) |
| API | **FastAPI (Python)** — not left open as "FastAPI or .NET" | FR-XPL-003 (one contract, three clients) |
| Core datastore | PostgreSQL with row-level security | IAM-002 (tenant isolation at data layer) |
| Ledger storage | Append-only posting tables, no UPDATE/DELETE grants on committed rows | FR-GL-003, CMP-009 |
| Document storage | Azure Blob, immutability policies, per-tenant keys (Azurite locally) | FR-DOC-005, IAM-004 |
| Search | **Postgres full-text search (`pg_trgm`)** — not OpenSearch | NFR-008 (archive search < 2s) |
| Async processing | Queue-based workers (OCR, matching, filing, notifications) | NFR-026 (graceful degradation) |
| Document AI | Managed extraction service in-region + tenant-scoped correction model | PRIV-015 |
| Analytics | Separate warehouse via CDC, pseudonymised | PRIV-014 |
| Web | React/TypeScript SPA (Vite) | — |
| Mobile | React Native + native modules | MOB-001, MOB-008 |
| Identity | Managed IdP for authN, in-house authZ | IAM-030 |
| Secrets | Azure Key Vault / managed HSM | SEC-022, SEC-025 |

## Alternatives considered

| Option | Rejected because |
|---|---|
| .NET for the API (PRD's other listed option) | Would split the API and the Python-native document-AI/OCR worker pipeline across two languages, widening the integration-adapter surface that architectural non-negotiable #4 (integrations sit behind adapters) is meant to keep narrow. FastAPI keeps one language across API and async workers. |
| OpenSearch for archive search (PRD's stated choice) | NFR-008 only requires <2s search over a 7-year archive per tenant. Postgres full-text search (`pg_trgm`/`pg_bigm`) on the already RLS-partitioned tables meets that without standing up a second datastore that would need its own tenant-partitioning scheme, its own per-tenant key isolation, and its own operational burden (patching, backups, another attack surface under SEC-030–039). Revisit if search requirements grow beyond NFR-008 (fuzzy ranking at scale, faceted search UI). |
| AWS (eu-west-1/eu-central-1) instead of Azure | Viable alternative for EU residency (PRIV-010); not chosen because the PRD's Azure choice was not in tension with any requirement — no reason to deviate. |
| OpenSearch/MinIO substituted 1:1 for Azure equivalents in local dev | Rejected in favor of Azurite specifically because it's the official Azure Blob emulator — using it locally means code written against blob storage behaves the same in Azure, rather than against an S3-compatible stand-in with different semantics. |

## Consequences

- The API and async worker codebase stay single-language (Python), simplifying local dev and
  hiring, but forecloses using .NET-specific tooling without a rewrite.
- Search is a Postgres feature, not a separate service — less operational surface now, but if
  product requirements exceed NFR-008 (e.g. relevance-ranked full-text search across millions of
  documents with faceting), this decision needs revisiting and would likely mean introducing
  OpenSearch after all.
- Everything else in the PRD §13 table stands as proposed; deviating from it later still requires
  showing which requirement the new choice serves, per [CLAUDE.md](../../CLAUDE.md).
