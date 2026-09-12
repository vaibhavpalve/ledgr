# ADR-007: WebAuthn passkeys — registration, authentication, revocation

- **Status**: Accepted
- **Date**: 2026-08-22

## Context

PRD §8.2's IAM-010 lists passkey as one of three sign-in methods offered side by side —
"Continue with Google, passkey, and email + password" — not a secondary or fallback option.
IAM-012 separately lists passkey as a supported second factor, but this task scoped the work to
IAM-010: passkey as a first-class sign-in method, supporting multiple named, individually
revocable passkeys per user. The MFA-composition question (when a passkey satisfies IAM-011's
"MFA is mandatory") is IAM-030+ territory — the permission/role system — and stays out of scope
here, same reasoning as `sessions.privileged` in ADR-005.

The design question: how to implement the WebAuthn/FIDO2 ceremonies (registration and
authentication) correctly — this is a protocol with real cryptographic subtlety (CBOR attestation
objects, COSE public keys, ECDSA/EdDSA/RSA signature verification, clone detection via signature
counters) — and how "individually revocable" is actually enforced rather than merely modeled as a
column.

## Decision

### Use `webauthn` (py_webauthn), not a hand-rolled implementation

The same reasoning already applied to Argon2 (passwords, ADR-005), PyJWT (Google ID tokens,
ADR-006), and `cryptography` (envelope encryption, ADR-004): WebAuthn's CBOR parsing, COSE key
decoding, attestation statement handling, and signature verification are exactly the kind of
low-level cryptographic protocol work where a maintained, widely-used library is the right call.
Before writing any application code, the library's actual v3.0.0 API was inspected directly
(`inspect.signature` on every function used) rather than relied on from memory, since WebAuthn
libraries have had breaking API changes across major versions — the dependency is pinned to
`webauthn>=3.0.0` specifically because that is the API surface this code was written and tested
against.

### Discoverable credentials with required user verification

Registration requests `resident_key=ResidentKeyRequirement.REQUIRED` and
`user_verification=UserVerificationRequirement.REQUIRED`. Both are deliberate, not defaults left
alone:

- **Discoverable/resident** is what makes the "click passkey, no email typed first" flow IAM-010
  implies actually work — the authenticator stores the credential such that it can be looked up
  by relying party alone, and the server resolves *which* user from the credential ID in the
  response (`complete_authentication` looks up by `credential_id`, never by an email the caller
  would otherwise have to supply first).
- **Required user verification** (not merely "user presence," i.e. a tap) is required because a
  passkey stands in for a password — IAM-010 places it beside email+password, not behind it, so
  the assurance level has to match. `require_user_verification=True` is passed to the library on
  both registration and authentication, and `verified.user_verified` is checked explicitly
  afterward too, so a future library behavior change that silently stopped enforcing this would
  fail loudly here rather than silently degrade.

### Clone detection: the library's own check, not reinvented

`webauthn.verify_authentication_response` already implements the correct signature-counter
comparison — `verify_authentication_response`'s source was read directly to confirm this before
relying on it: it raises unless `new_sign_count > stored_sign_count`, *except* when both are 0,
which is the normal, permanent state for many platform/synced authenticators (Face ID/Touch ID
with iCloud Keychain, Android's Google Password Manager passkeys) that never implement counting at
all. Reimplementing this check independently would risk getting that "0 is normal, not a clone
signal" exception subtly wrong — using the library's own logic, verified by reading its source, is
safer than parallel bespoke logic that could drift from it.
`tests/auth/test_passkeys.py::test_a_sign_count_permanently_at_zero_is_not_treated_as_a_clone` and
`::test_a_sign_count_that_fails_to_increase_is_rejected_as_a_possible_clone` exercise both branches
directly, using `FakeAuthenticator(sign_count_increments=False)` to simulate the permanently-zero
case honestly rather than assuming it.

### Revocation is checked before cryptographic verification, and is per-row

`user_passkey.revoked_at` is set by `revoke_passkey`, which never touches any row but the one
targeted — one passkey's revocation has zero effect on a user's other passkeys (tested directly:
`test_revoking_one_passkey_does_not_affect_another`). `complete_authentication` checks
`passkey.is_revoked` **before** calling into the cryptographic verification at all — a revoked
passkey is rejected unconditionally, regardless of whether its signature would otherwise have
verified, and costs nothing extra (no wasted signature check) to reject.

`revoke_passkey` requires the caller to supply both the passkey id and the acting user's id, and
checks `passkey.user_id == user_id` before revoking — raising the identical `PasskeyNotFoundError`
whether the passkey doesn't exist at all or belongs to someone else, so neither failure mode
discloses more than the other (the same SEC-008-flavored reasoning already applied in ADR-005 and
ADR-006). Tested directly with an explicit cross-user revocation attempt.

### Registration/authentication challenges are not persisted by this module

Same pattern as `api.auth.google_oidc` (ADR-006): `begin_registration`/`begin_authentication`
return the challenge to the caller rather than owning storage for it. There is no login HTTP
endpoint yet to decide where that server-side flow state lives (a session record, not a
client-readable cookie) — deliberately left open, consistent with the rest of this auth package.

### Test ceremonies are real, not mocked

`tests/auth/webauthn_helpers.py`'s `FakeAuthenticator` generates a real P-256 EC keypair, builds a
real CBOR-encoded COSE public key and authenticator data byte-for-byte per the WebAuthn spec, and
produces a real ECDSA-SHA256 signature over the actual bytes a browser's authenticator would sign.
Every test in `test_passkeys.py` exercises `webauthn.verify_registration_response`/
`verify_authentication_response` for real — including the forged-signature test, which signs the
same payload with an unrelated key to prove verification actually checks the stored public key,
not merely that a well-formed signature is present. This was validated end-to-end in a scratch
script against the real library before being written into test files, the same rigor already
applied to the hand-signed RS256 JWTs in ADR-006's Google ID token tests.

Full implementation: [migrations/0005_webauthn_passkeys.sql](../../apps/api/migrations/0005_webauthn_passkeys.sql),
[src/api/auth/passkeys.py](../../apps/api/src/api/auth/passkeys.py).

## Alternatives considered

| Option | Rejected because |
|---|---|
| Hand-roll CBOR/COSE parsing and signature verification | Enormous, security-critical surface to get right from scratch (attestation formats, COSE key types, algorithm negotiation) for no benefit over a maintained library already used by many production systems. |
| `user_verification=PREFERRED` (the library's own default) instead of `REQUIRED` | A passkey that only proves presence, not identity, is a weaker guarantee than the password it is meant to stand beside in IAM-010's UI — `PREFERRED` would let some authenticators skip verification silently. |
| A generic `user_credential` table spanning Google, password, and passkey methods | Same reasoning as ADR-006's equivalent alternative: each method's data shape differs enough (a passkey needs a public key, sign count, and transports; Google needs only a subject; a password needs only a hash) that a shared polymorphic table would be awkward now for a generalization not yet needed. |
| Reimplement sign-count/clone-detection comparison independently of the library, for a more specific exception type (e.g. a dedicated `SuspectedClonedAuthenticatorError`) | The library's own check, read directly from source, is correct and already covers the "0 is normal" exception properly. A parallel implementation risks drifting from it over a library upgrade; wrapping its exception into the single `PasskeyAuthenticationError` (with the original message preserved via chaining) was judged safer than maintaining a second copy of security-critical comparison logic. |
| Allow a caller-supplied `sign_count` to bypass the counter check for testing convenience | Would mean the test suite validates a code path different from what production runs. `FakeAuthenticator` instead models realistic authenticator behavior (incrementing or permanently-zero) and lets the real library logic run unmodified in every test. |

## Consequences

- No HTTP endpoint or UI exists — `WebAuthnService` is the layer a future registration/login flow
  calls, with the same open question ADR-006 left for Google: where challenge state lives between
  `begin_*` and `complete_*` across two HTTP requests.
- `transports`, `aaguid`, `backup_eligible`, and `backed_up` are captured and stored but nothing
  yet acts on them (e.g. treating a synced/backed-up passkey as a different trust tier than a
  hardware-bound one). They exist because the registration response already carries them at zero
  extra cost to capture, not because a consuming feature exists yet.
- IAM-012's "does a passkey satisfy MFA" question, and IAM-011's "which actions require it,"
  remain unanswered — both depend on the not-yet-built role/permission system to know when a
  write-capable action needs to check.
