# Threat model — PRD §9.1 mapping (SEC-050)

Scope: this covers what exists in the repository today (`apps/api`, `apps/web`, `packages/shared-types`, CI). There
is no infrastructure-as-code, no Azure deployment, and no container build pipeline in this repo yet — so any
control that lives at the infra/platform layer (network segmentation, WAF, KMS/HSM, SIEM, backups) is reported as
**Missing** here not because it was tried and failed, but because there is nothing to evaluate: it hasn't been
built. That's a scope gap, not a defect, but it means the threats that are mostly infra-shaped (ransomware, parts
of insider abuse, egress detection) are largely open.

Status legend: **Implemented** (real control, proven by a real test) · **Partial** (built but incomplete, unwired,
or stubbed) · **Missing** (no code exists).

---

## 1. Account takeover

PRD controls: passkeys, mandatory MFA, breached-credential screening, anomaly detection, device session management.

| Sub-control | Status | Where it lives | Proof | Gap |
|---|---|---|---|---|
| Passkeys | Implemented | [`auth/passkeys.py`](apps/api/src/api/auth/passkeys.py) — `WebAuthnService.begin_registration`/`complete_registration` (~L198-270), `complete_authentication` (~L301-347), `revoke_passkey` (~L352-364) | [`tests/auth/test_passkeys.py`](apps/api/tests/auth/test_passkeys.py): `test_register_then_authenticate_round_trip`, `test_a_forged_signature_from_a_different_key_is_rejected`, `test_revoking_one_passkey_blocks_authentication_with_it`, `test_a_sign_count_that_fails_to_increase_is_rejected_as_a_possible_clone` | Service layer only — **no HTTP route** wires this into an actual login flow. `main.py` registers no auth routes at all. |
| Mandatory MFA | Implemented | [`auth/mfa.py`](apps/api/src/api/auth/mfa.py) `AlwaysRequireMfaPolicy` (~L140-142); enforced globally by [`mfa_middleware.py`](apps/api/src/api/mfa_middleware.py) `MfaEnforcementMiddleware.dispatch` (~L92-134), added in [`main.py:87`](apps/api/src/api/main.py#L87) with no per-route opt-out | [`tests/test_mfa_middleware.py`](apps/api/tests/test_mfa_middleware.py): `test_unenrolled_user_is_blocked_before_the_handler_runs`, `test_enrolled_but_unverified_user_is_blocked_with_a_different_reason`, `test_verified_user_reaches_the_handler_even_with_no_enrolled_factor` | "Mandatory for everyone, always" is a deliberate stand-in for IAM-011's real scope (write-permission holders / filing administrations) — over-conservative today because the role system it should key off isn't finished. Correct direction, not final shape. |
| Breached-credential screening | Implemented | [`auth/breach_check.py`](apps/api/src/api/auth/breach_check.py) `HibpBreachChecker.is_breached` (~L57-73, real k-anonymity SHA-1 prefix/suffix against the HIBP API); called from `AuthenticationService.register_user` and `AccountRecoveryService._finish_recovery` | [`tests/auth/test_passwords.py::test_breached_password_is_rejected_even_if_long_enough`](apps/api/tests/auth/test_passwords.py) | Only checked at signup and password recovery — **there is no password-change endpoint yet**, so an existing password can't be re-screened. Also: `LocalDenylistBreachChecker` exists as a fallback and would silently downgrade this control if `provider` is misconfigured in prod — nothing currently guards against that misconfiguration. |
| Anomaly detection | Partial | The only real "anomaly" signal is credential-stuffing velocity in [`auth/rate_limiting.py`](apps/api/src/api/auth/rate_limiting.py) `AuthRateLimiter.record_attempt` (~L178-201) — alerts when ≥10 distinct accounts fail from one IP in 5 minutes. [`auth/geolocation.py`](apps/api/src/api/auth/geolocation.py) is a display-only IP→city lookup with no baseline or deviation logic. | [`tests/auth/test_rate_limiting.py`](apps/api/tests/auth/test_rate_limiting.py): `test_one_ip_failing_against_many_distinct_accounts_triggers_an_alert`, `test_fewer_than_the_threshold_of_distinct_accounts_does_not_alert` | This is *credential-stuffing detection*, not the "impossible travel / new device" anomaly detection the PRD term usually means. No per-user login-pattern baseline, no device fingerprinting. `LoggingAnomalyAlerter` only writes a log line — **no alerting/paging pipeline exists**, and this middleware isn't wired into `main.py` at all (see §3 below), so today it fires on nothing in production. |
| Device session management | Implemented | [`auth/sessions.py`](apps/api/src/api/auth/sessions.py) `SessionService.list_sessions`, `revoke_session`, `revoke_all_for_user` (~L174-208); idle timeout + 12h absolute lifetime for privileged sessions (`validate_session`, ~L126-147) | [`tests/auth/test_sessions.py`](apps/api/tests/auth/test_sessions.py): `test_list_sessions_reflects_reality_for_iam_017`, `test_revoke_all_for_user_revokes_only_that_users_sessions`, `test_idle_timeout_applies_to_privileged_sessions` | No device-trust/fingerprint identity — a session is a hashed bearer token plus `user_agent`/`ip_address` fields, not a persistent "known device" concept, so "new device" can't currently be distinguished from "same device, new IP." |

**Bottom line**: the primitives (passkeys, MFA enforcement, breach screening, session revocation) are genuinely solid
and well-tested in isolation. The weak points are (a) none of this is reachable via an actual HTTP login endpoint
yet, and (b) "anomaly detection" is narrower than the PRD implies and has no alert delivery.

---

## 2. Cross-tenant data leakage

PRD controls: row-level security, per-tenant keys, mandatory isolation tests, no shared caches keyed without tenant.

| Sub-control | Status | Where it lives | Proof | Gap |
|---|---|---|---|---|
| Row-level security | Implemented | [`migrations/0001_tenancy_core.sql`](apps/api/migrations/0001_tenancy_core.sql) ~L180-186 (`app.current_org_id()`, fail-closed on NULL), ~L248-261 (`FORCE ROW LEVEL SECURITY` on organization/administration/firm_engagement/fiscal_year/period); `migrations/0020_ledger.sql` ~L1482-1531 (same for ledger tables). Tenant context set per-request via `SET LOCAL app.current_org_id` in [`db.py:57-61`](apps/api/src/api/db.py#L57) — transaction-scoped, so it can't leak across a pooled connection. | [`tests/integration/test_administration_isolation.py`](apps/api/tests/integration/test_administration_isolation.py): `test_list_administrations_does_not_leak_other_tenants`, `test_get_administration_hides_other_tenants_record`; `tests/integration/test_encryption_key_isolation.py::test_org_a_cannot_see_org_bs_wrapped_key` | None found — these tests genuinely open a second tenant's real Postgres session and assert the foreign row is *absent*, not just that a WHERE clause exists. |
| Append-only ledger (CLAUDE.md rule 2) | Implemented | `migrations/0020_ledger.sql` ~L1401-1425: `ledgr_app` role has `SELECT` only on posting tables, no INSERT/UPDATE/DELETE/TRUNCATE grants; ~L720-754 unconditional `RAISE` triggers on UPDATE/DELETE/TRUNCATE as defense-in-depth | [`tests/integration/test_ledger_bounded_context.py`](apps/api/tests/integration/test_ledger_bounded_context.py) connects literally as `ledgr_app` and asserts every write form (including `ON CONFLICT DO UPDATE`, `TRUNCATE ... CASCADE`) is rejected by Postgres privileges | None — this is privilege-level enforcement, not app-layer discipline. |
| Mandatory isolation tests (IAM-005) | Implemented | [`tests/test_isolation_coverage.py`](apps/api/tests/test_isolation_coverage.py) ~L20-29 introspects the live FastAPI route table and fails if any route lacks an `@pytest.mark.isolation(method, path)` marker | `test_isolation_coverage.py::test_every_route_has_isolation_coverage` | The gate proves a test *exists* per route, not that it's a good test. Spot-checked `test_administration_isolation.py` — it's genuine (real second-tenant session, asserts by absence). Worth periodically auditing that the marker isn't satisfied by a shallow test as the route count grows. |
| No shared caches keyed without tenant | N/A — no cache exists | Grepped `apps/api/src` for `redis`, `lru_cache`, `cachetools`, in-process dict caches: zero hits | — | Nothing to leak today, but also nothing tested for this. **Flag this in the PR that introduces the first cache** — there's no existing pattern or test harness to copy. |

**Bottom line**: this is the most mature area in the codebase. Real DB-enforced RLS, a real distinct-role privilege
boundary for the ledger, and a structural CI gate that a new route can't slip through without an isolation test.

---

## 3. Insider access abuse

PRD controls: no standing access, JIT approval, session recording, customer-visible access log.

| Sub-control | Status | Where it lives | Proof | Gap |
|---|---|---|---|---|
| No standing access / JIT approval | **Missing** (mostly) | Grants are standing by default — `expires_at` is nullable and persists until explicit revocation. [`authz/firm_staff.py`](apps/api/src/api/authz/firm_staff.py) `FirmStaffGrant.expires_at`/`is_live` (~L71-82) lets a grant *optionally* carry an expiry, opt-in only. [`authz/sod.py`](apps/api/src/api/authz/sod.py)'s `NO_SELF_APPROVED_ACCESS_REQUEST` rule (~L287-293) references an `"access_request"` resource type — but no service, repository, or route implementing an access-request/approval workflow exists anywhere. | None — there's no workflow to test | This is a real gap, not a nuance: there is no time-boxed elevation, no approval step, no default expiry. The SoD rule is guarding a feature that doesn't exist yet. |
| Session recording | **Missing** | Exhaustive grep for "session recording"/"screen recording"/"replay" across `apps/api/src` found only unrelated hits (idempotency replay, WebAuthn/TOTP/OIDC cryptographic replay protection) | — | Zero code presence. This is normally a PAM/bastion-host product, not something built in an API codebase — treat as an infra/tooling procurement decision, not a backlog item here. |
| Customer-visible access log | Partial | Backend is solid: [`audit/repository.py`](apps/api/src/api/audit/repository.py) `SqlAuditRepository.search` (~L159-194, tenant-scoped, RLS-backed) and `.verify_chain`/`.chain_head` (~L122-157, hash-chained for tamper evidence); [`authz/firm_access_register.py`](apps/api/src/api/authz/firm_access_register.py) `FirmAccessRegister.list_firm_access` (~L124-139) gives visibility into which firm users hold access and when they last used it (IAM-109). **But there is no HTTP route for either** — no `routes.py` under `audit/` or the access register, and `main.py` registers no such router. | [`tests/audit/test_audit_log.py::test_search_never_crosses_a_tenant_boundary`](apps/api/tests/audit/test_audit_log.py); [`tests/integration/test_audit_tamper_evidence.py::test_the_repository_exposes_no_way_to_modify_or_remove_an_entry`](apps/api/tests/integration/test_audit_tamper_evidence.py) | The engine and data model are genuinely customer-scoped and tamper-evident, proven under adversarial DB-role tests. It is simply not reachable by a client yet — backend-only. |
| Segregation of duties (adjacent control) | Implemented | [`authz/sod.py`](apps/api/src/api/authz/sod.py) — 5 rules (creator-not-sole-approver, approver-not-releaser, submitter-not-approver, no-self-grant, no-self-approved-access-request), owner-only deviation workflow with audit disclosure (~L406-422, L614-676) | [`tests/authz/test_sod.py`](apps/api/tests/authz/test_sod.py) — 40+ tests, e.g. `test_the_creator_cannot_provide_the_only_approval`, `test_a_deviation_requires_a_reason` | Thoroughly built and tested, but only one rule (`NO_SELF_GRANT`, inside `AuthorizationService.assign_role`) has a live caller today. The other four are enforcement points waiting for the invoice/payment-batch/expense-approval features (FR-AP, FR-EXP) that don't exist yet — see Threat 4. |

**Bottom line**: the access-log *data layer* and SoD *engine* are ahead of the features that will call them. The
actual insider-abuse story today is "standing access, no JIT, and nothing customer-facing to look at" — the three
strongest named controls in the PRD row are unbuilt or unreachable.

---

## 4. Invoice fraud / IBAN swap

PRD controls: supplier bank-detail change alerts, SoD on payment release, out-of-band change verification.

| Sub-control | Status | Where it lives | Proof | Gap |
|---|---|---|---|---|
| All three | **Missing** | No payments/PSP/supplier-payment module exists in the codebase at all (confirmed: no `payments/`, `psp/`, or `banking/` package; `bank_account`/IBAN hits are all the *seller's own* IBAN printed on outgoing invoices in `invoicing/`, not a payables/payment-release flow) | — | There is nothing to attack yet because there's no accounts-payable / payment-initiation feature. The SoD engine (§3) is ready to enforce approver-not-releaser once that feature exists — that's the one piece of forward preparation already in place. |

**Bottom line**: entirely unbuilt. Not a regression, just out of scope so far — flag as a hard SEC-050 blocker
**before** any payables/payment feature ships, since none of the three named controls have a home yet.

---

## 5. Malicious document upload

PRD controls: sandboxed parsing, malware scanning, content-type verification, no server-side rendering of untrusted content.

| Sub-control | Status | Where it lives | Proof | Gap |
|---|---|---|---|---|
| Sandboxed parsing | **Missing** | [`documents/service.py:190`](apps/api/src/api/documents/service.py#L190) calls the scanner in-process — no subprocess isolation, no network/credential restriction | — | No sandbox mechanism exists; scanning and content-sniffing both run inside the API process on raw uploaded bytes. |
| Malware scanning | **Partial / stub** | [`documents/scanning.py`](apps/api/src/api/documents/scanning.py) ~L91-109 `LocalPatternScanner` detects only the literal EICAR test string and is explicitly documented in its own code as "NOT a malware scanner"; `build_scanner()` (~L127-140) raises `ValueError` for any provider other than `"local"` — the `MalwareScanner` protocol seam for a real AV engine (ClamAV, cloud API) exists but nothing implements it | [`tests/documents/test_service.py`](apps/api/tests/documents/test_service.py) — exercises the pipeline logic against `LocalPatternScanner`/`RefusingScanner` | The scan-before-store pipeline and infected/failed/pending state handling are real and well-tested; the actual malware detector is not. **This is the single most exploitable gap that looks covered but isn't** — a real AV engine must be wired into `build_scanner` before this control does anything in production. |
| Content-type verification by magic bytes | Implemented | [`documents/content_type.py`](apps/api/src/api/documents/content_type.py) ~L140-155 `sniff()` reads leading bytes against a closed allowlist (JPEG/PNG/PDF/HEIC); ~L174-201 `verify_declared()` ignores the client's declared Content-Type/filename entirely; explicit refusals for SVG/HTML/ZIP/executables (~L113-127) | [`tests/documents/test_content_type.py`](apps/api/tests/documents/test_content_type.py) | None found — filename/extension is never consulted in the upload path. |
| No server-side rendering of untrusted content | Implemented by omission | No thumbnail/preview/PIL/pdf2image/ImageMagick code anywhere in `documents/`; download path in `documents/routes.py` forces `Content-Disposition: attachment` | — (no dedicated test, but absence is structural — nothing to render) | Correct today because no rendering feature exists; re-verify this line if/when a document-preview feature is proposed. |

**Bottom line**: the parts that are implemented (content-type sniffing, refuse-on-scan-failure state machine) are
implemented well. The part actually named "malware scanning" is a labelled stub — treat this control as **not
present** for threat-modelling purposes until a real AV engine is wired in.

---

## 6. Supply chain compromise

PRD controls: SBOM, pinned dependencies, signed builds, provenance attestation.

| Sub-control | Status | Where it lives | Proof | Gap |
|---|---|---|---|---|
| Pinned dependencies | Implemented | `apps/api/uv.lock`, `pnpm-lock.yaml` committed; CI uses `uv sync --frozen` / `pnpm install --frozen-lockfile` ([`.github/workflows/ci.yml`](.github/workflows/ci.yml) lines 62, 78, 93, 109, 124, 140, 158, 196, 209, 225, 241) | CI fails if the lockfile and manifest disagree | None. |
| Dependency/secret scanning (adjacent, SEC-051) | Implemented | `ci.yml` `secret-scan` job (Gitleaks), `dependency-scan-web` (`pnpm audit --audit-level=high`), `dependency-scan-api` (`uv run pip-audit --strict`) — all required by the aggregate `ci` job | The `ci` job fails the whole pipeline (`needs:` list) if any of these fail | None for the CI gate itself; note this is dependency vulnerability scanning, not SAST — there's no static-analysis-for-vulnerabilities step (`ruff`/`mypy` here are lint/type checks, not security SAST). |
| SBOM | **Missing** | No SBOM-generation step anywhere in `ci.yml` | — | Nothing generates or retains a CycloneDX/SPDX SBOM per release. |
| Signed builds | **Missing** | No signing step, no `cosign`/`sigstore` usage in the repo | — | There's also no container build at all in this repo yet (`docker-compose.yml` only runs Postgres and the Azurite emulator for local dev) — so "signed builds" has no artifact to sign yet. |
| Provenance attestation | **Missing** | No SLSA/provenance step in CI | — | Same as above — needs a real build/release pipeline before this is meaningful. |

**Bottom line**: the CI-gate half of supply-chain security (pinning, secret scanning, dependency scanning) is real
and enforced. The release-integrity half (SBOM, signing, provenance) doesn't exist, largely because there's no
build/release pipeline to attach it to yet.

---

## 7. Data exfiltration via API

PRD controls: scoped tokens, rate limits, export logging, egress anomaly detection.

| Sub-control | Status | Where it lives | Proof | Gap |
|---|---|---|---|---|
| Scoped tokens | **Missing** | No API-key/PAT object anywhere in `auth/` or `authz/`. Auth is a JWT bearer carrying `org_id`/`sub`/`sid`/`mfa_verified`/`adm` claims ([`tenancy.py`](apps/api/src/api/tenancy.py) ~L73-128) plus DB-backed sessions — "scope" elsewhere in the codebase (`authz/matrix.py`, `authz/profiles.py`) means authorization scope (which org/administration), not token scope | — | There is no way today to issue a third-party integration a token limited to, say, read-only invoice access — every authenticated caller carries the same session-shaped credential. |
| Rate limits | **Partial** | [`rate_limit_middleware.py`](apps/api/src/api/rate_limit_middleware.py) implements real per-account and per-IP limiting with progressive lockout, but only for a fixed `protected_paths` map of auth endpoints (login, recovery, TOTP) — and per its own module docstring, and confirmed by reading [`main.py`](apps/api/src/api/main.py#L85-89), **it is not added to the app at all**. The live middleware stack is `AuthorizationEnforcementMiddleware`, `IdempotencyMiddleware`, `MfaEnforcementMiddleware`, `AuditMiddleware`, `TenantContextMiddleware` — no rate limiter. | [`tests/test_rate_limit_middleware.py`](apps/api/tests/test_rate_limit_middleware.py), [`tests/auth/test_rate_limiting.py`](apps/api/tests/auth/test_rate_limiting.py) — tested in isolation | Dead code in production today: there are no real auth HTTP endpoints for it to protect yet, and no general data-read/export endpoint is rate-limited at all. **This needs to be added to `main.py`'s middleware stack**, not just exist as a module. |
| Export logging | **Missing** | No bulk/CSV/GDPR export feature exists anywhere in `apps/api/src` (grepped for `csv`, `gdpr`, `data_export`, `export_administration`) | — | Per-record reads of financial data *are* audited (`AuditCategory.FINANCIAL_READ` in `documents/service.py`), but there's no bulk-export capability yet, so there's nothing distinct to log. Genuine capability gap, not an instrumentation gap. |
| Egress anomaly detection | **Missing** | Grepped for `egress`, `siem`, `dlp` — no hits outside the credential-stuffing detection already covered in Threat 1 | — | Correctly out of scope for an API codebase; this is a SIEM/network-egress-monitoring concern that belongs at the infra layer, which doesn't exist in this repo yet. |

**Bottom line**: this is the weakest-covered threat in the table. No scoped tokens, the one rate limiter that exists
isn't wired into the app, and there's no export feature to log in the first place.

---

## 8. Ransomware

PRD controls: immutable backups, isolated backup credentials, restore drills.

| Sub-control | Status | Where it lives | Proof | Gap |
|---|---|---|---|---|
| All three | **Missing** | No backup configuration, no IaC, no Azure resources in this repo | — | This is entirely an infrastructure/operations concern (SEC-038, SEC-039) that has no counterpart in an application codebase — there is nothing to point to yet because there's no deployed environment. Cannot be closed by writing more application code; needs an infra workstream. |

---

## Honest gap summary (ranked by how exploitable the gap actually is)

1. **Malware scanning is a labelled stub** (§5) — the code *looks* like a real scanning pipeline (state machine, refuse-on-fail, well-tested), but the actual detector only catches the EICAR test string. This is the gap most likely to give false confidence in a review that only reads test names.
2. **No feature is reachable end-to-end via HTTP yet for auth** — passkeys, MFA, breach-checking, and session management are all real at the service layer, but `main.py` registers no login/auth routes, so none of this is live in an actual client-facing API today.
3. **Rate limiting exists but isn't wired in** (§7) — `rate_limit_middleware.py` is real and tested but absent from `main.py`'s middleware stack, so it currently protects nothing in production.
4. **No JIT/time-boxed access, no customer-facing access log route** (§3) — the three strongest named insider-abuse controls (no standing access, JIT approval, customer-visible log) are either unbuilt or built-but-unreachable. Standing access is the default today.
5. **Invoice fraud / IBAN swap and Ransomware are 0% built** (§4, §8) — no payments feature exists yet, and there is no infrastructure layer in this repo to hold backups, so neither has anything to evaluate against. These are scope gaps, not regressions, but should stay visible as launch blockers rather than quietly falling off the list.
6. **No scoped API tokens, no export feature/logging** (§7) — third-party integrations and bulk data export are both unbuilt, so there is no scoping or logging story for them yet.
7. **Supply-chain release integrity (SBOM, signed builds, provenance)** (§6) — CI enforces pinning, secret scanning, and dependency scanning today, but nothing generates an SBOM or signs anything, mostly because there's no release/build pipeline yet to attach it to.
8. **No caching layer exists** (§2) — currently a non-issue (nothing to leak), but there is no established tenant-safe caching pattern or test harness ready for the day one gets added.

The most mature area by a clear margin is **cross-tenant isolation** (§2): real RLS enforced against actual
Postgres roles, a distinct DB role for the ledger with no write grants, and a CI-enforced rule that no route can
ship without an isolation test. If anything in this codebase should be held up as the template for how the other
threats ought to be closed, it's that one.
