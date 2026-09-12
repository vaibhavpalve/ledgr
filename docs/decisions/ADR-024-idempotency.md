# ADR-024: Idempotency keys on every mutating endpoint

- **Status**: Accepted
- **Date**: 2026-09-01
- **Implements**: NFR-032 (PRD §12)
- **Related**: [ADR-022](ADR-022-ledger-bounded-context.md) — the domain-level key on `journal_entry`

## Context

> **NFR-032.** Idempotency keys on all mutating API endpoints so retries cannot double-post.

One mutating endpoint exists today (`PUT /v1/switcher/{administration_id}`). So the deliverable is
not "protect that route" — it is infrastructure that binds every endpoint that has not been written
yet, because that is where the double-posting will actually happen.

## Decision

### Middleware for every mutating method, not a per-route declaration

The other three "cannot forget" guarantees here are **opt-in per route**: a route names its
permission, and names an audit category, and a coverage check fails the build when it names
neither. That shape is right when the route knows something the middleware cannot.

Idempotency knows nothing route-specific. Every mutating endpoint wants identical behaviour, so a
per-route declaration would be pure ceremony — and a new endpoint would be unprotected until someone
performed it. Applying it to every `POST`/`PUT`/`PATCH`/`DELETE` inverts what can go wrong: a new
endpoint is covered the moment it is registered, and **opting out** is what takes a deliberate act.

So `tests/test_idempotency_coverage.py` is shaped differently from its three siblings. It does not
check that routes declare something; it checks that the middleware is installed, that it sits in the
right place in the chain, and that the opt-out list is empty. The list is empty today, and the test
prints every entry in its failure message so adding one is read by a reviewer.

### Two layers, and neither subsumes the other

| | Scope | Lifetime |
|---|---|---|
| `idempotency_key` (0022) | every mutating endpoint | 24 hours |
| `journal_entry.idempotency_key` (0020) | postings only | forever |

The HTTP layer stops the *request* from executing twice and gives the caller back the same response
body. The domain layer stops a *second posting* for the same key even when the request does execute
again — because the key expired, or because the caller came in through a path with no HTTP edge at
all. Neither covers the other's ground.

### The four outcomes

    CLAIMED     first sighting of this key; run the handler
    REPLAY      completed, fingerprint matches; serve the stored response
    IN_FLIGHT   a duplicate is executing right now → 409
    MISMATCH    the key was reused for a DIFFERENT request → 422

**MISMATCH is checked before state**, and that ordering is load-bearing. A key reused for a
different request is a client bug whether or not the first one has finished, and reporting it as
"still in flight" would send the caller into a retry loop that can never succeed — the fingerprint
will never match.

422 rather than 409 follows `draft-ietf-httpapi-idempotency-key-header`: the request does not
conflict with server state, it contradicts an earlier request that claimed the same key. Serving the
first response instead would be silently wrong in the most expensive way available — the caller
would believe their second, *different* operation had succeeded.

### What a key identifies, and what the fingerprint covers

The unique constraint is `(organization_id, user_id, method, path_template, key)`. `user_id` is in
there because the stored body is whatever the *first* caller was allowed to see: a key shared across
users would be a cross-user data leak wearing an idempotency key.

The **path template** is part of the key; the **concrete path** is part of the fingerprint. So
reusing a key against a different administration is reported to the caller as a bug, rather than
quietly becoming a separate key that executes twice — which is precisely the failure that would
double-post to the wrong client (FR-FRM-000a's harm, arriving by another route).

Fingerprint fields are length-prefixed, for the reason 0019's audit hash is: without it adjacent
fields run together and `/ab` + `c` collides with `/a` + `bc`.

### Failure handling: 5xx releases, 4xx is stored

A **4xx is a deterministic answer to this exact request** — a validation error, a denial, a
conflict. Replaying it is correct: the retry would compute the same one, and serving the stored copy
keeps a blindly-retrying client from hammering an endpoint that will keep saying no.

A **5xx is not deterministic**, so the claim is released and a retry is a real retry. Storing it
would make a transient failure permanent for the life of the key: the caller retries correctly and
receives the same 500 forever. The request's own transaction has already rolled back, so nothing was
committed that a second attempt could duplicate.

### Expiry is honoured by the claim, not by the cron job

`app.claim_idempotency_key` deletes an expired row *for the key it is claiming* before inserting.
Without that, an expired row still conflicts, the read finds it, and an expired key is replayed
forever — expiry would mean nothing until a purge job happened to run, making correctness depend on
a schedule. The job (`scripts/purge_idempotency_keys.py`) only reclaims space.

That job is cross-tenant and runs as `ledgr_ops`. `ledgr_app` holds no `EXECUTE` on it, and
`SqlIdempotencyRepository` has no `purge_expired` method — a request handler that could delete other
tenants' keys is a capability no endpoint needs.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Declare idempotency per route, like permissions and audit | Nothing route-specific to declare, so it is ceremony — and a new endpoint is unprotected until someone performs it. |
| Serve the first response when the key is reused for a different request | Silently wrong in the most expensive way: the caller believes their second, different operation succeeded. |
| Key on `(organization, method, path, key)` without `user_id` | The stored body is one caller's data. Another user in the same tenant could replay it. |
| Put the concrete path in the KEY rather than the fingerprint | Reusing a key against a different id would become a separate key that executes twice, instead of an error the caller hears about. |
| Claim inside the request's transaction | Invisible to a concurrent duplicate until the first request finishes — precisely the window it exists to close. |
| `SELECT` then `INSERT` | Two concurrent first attempts both see nothing and both execute; one fails on the unique constraint at commit, *after* the work. |
| Store 5xx responses too | Makes a transient failure permanent for the life of the key. |
| Release the key on a 4xx as well | A denial is deterministic; re-executing it gains nothing and lets a retry loop hammer the endpoint. |
| No expiry at all | Unbounded growth, and a key could never be reused. The trade is stated rather than hidden: after the window, a retry re-executes. |
| Rely on the ledger's own key alone | Covers postings only. Every other mutating endpoint would be unprotected. |

## Consequences

- **30 DB-free tests and 14 DB-gated ones.** The pure suite drives a probe app whose handler counts
  its own executions — "cannot double-post" is a statement about that counter, and every assertion
  reduces to it.
- **20 guards mutation-tested, all 20 caught.** Two of those initially survived and both were the
  tests' fault, again: the length-prefix test used `/a/b`+`c` against `/a`+`b/c`, which do **not**
  collide under bare concatenation, so it passed whether or not the prefixing was there; and
  nothing in the pure suite covered `release()` dropping a *completed* key, which would let a
  successful request execute twice. Both are fixed and now fail when the guard is removed.
- **The atomicity test needs a real database.** `INSERT … ON CONFLICT DO NOTHING` is what makes
  exactly one of twenty concurrent claims win; a `SELECT`-then-`INSERT` written by someone who did
  not know that would pass every in-memory test in the suite and double-post under load.
- **Requiring a key is a breaking change for existing callers**, and it landed as one: two
  DB-gated switcher tests had to start sending `Idempotency-Key`. `PUT /v1/switcher/{id}` is
  naturally idempotent and is still **not** exempt — "harmless today" is a property of the current
  handler, not of the endpoint, and the exemption would outlive the reasoning.
- **Responses are buffered to be storable.** A response already streamed to the client cannot also
  be written to the store. That bounds what this middleware can wrap — a large download would be
  held in memory — which is a reason mutating endpoints should not stream large bodies, and is why
  only mutating methods reach the buffering path at all.
- **The handler is not made transactional.** If a handler commits and then the response fails to
  serialise, the key is released and a retry re-executes. The domain-level key makes that safe for
  postings; nothing makes it safe for an endpoint that has no such key, which is a reason to give
  future mutating endpoints one.
- **`0022_idempotency.sql` has not been executed here.** Parse-checked only — `$$` balance, every
  signature resolved, 19 statements parsed. CI applies it and runs the 14 DB-gated tests for real.
  **Until that run is green, the atomicity claim is designed, not demonstrated** — and it is the
  claim the whole requirement rests on.
