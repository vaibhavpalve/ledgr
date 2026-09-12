# ADR-019: The client switcher and the active-client marker

- **Status**: Accepted
- **Date**: 2026-08-27
- **Implements**: FR-FRM-000, FR-FRM-000a (PRD §5.2)

## Context

> **FR-FRM-000.** Client switcher: searchable by client name, KvK number or trade name,
> keyboard-reachable, showing only granted administrations. Switching preserves the current screen
> type where it exists for the target client.
>
> **FR-FRM-000a.** The active client is unmistakable at all times — persistent name and colour marker
> in the header on every screen, on web and mobile. Posting to the wrong client is the single worst
> usability failure in this product.

FR-FRM-000a names a **harm**, not a widget. That framing decides the whole design: the answer cannot
be only a coloured badge, because a badge makes the failure less likely and never impossible. This is
the first requirement in this codebase where the product surface and a correctness control are the
same feature.

## Decision

### Three mechanisms, in descending order of how much they actually protect

1. **A mutating request naming a different client from the one the session has open is refused.**
   `require_permission` returns 409 before the handler runs. If the header says Bakker and the
   request posts to De Vries, one of them is wrong and the write does not happen. This is the only
   mechanism here that *prevents* the failure.
2. **The header badge is derived from the session's active client**, never from a parameter the
   screen passes in. A header taking its name from the page's own state could drift from what the
   page posts to; one derived from the session cannot.
3. **The colour marker**, which makes a wrong client noticeable at a glance before anything is
   submitted.

### The guard runs *before* authorization, and answers 409 rather than 403

Before, because the dangerous request is the one that would otherwise **succeed** — a caller lacking
permission is refused anyway, so checking the mismatch only after a grant check would skip it exactly
when it matters. 409 rather than 403 because a mismatch is client-state desynchronisation, not a
permission problem; answering 403 would send whoever debugs it looking at grants.

Reads are never constrained. Opening a client's page from a link or a search result is ordinary, and
refusing it would make the product unusable to protect against nothing — the named failure is
*posting*.

A session with no active client (a non-interactive API caller, or a session at the switcher)
constrains nothing. That is not a bypass: this is a wrong-client safety control for the first-party
UI, not an authorization control. Whether a caller may touch an administration at all is already
settled by `authorize()`, and omitting the claim gains them nothing there.

`allows_cross_client` defaults to False, so the safe behaviour is what a route gets by not thinking
about it. The one route that legitimately writes to a different client — the switcher, whose entire
job is changing which that is — says so at its own definition site rather than in a list elsewhere.

### Colour is a token, never the only signal, and its collisions are reported

`colour_token` is `'amber'`, not a hex value: the clients decide what amber looks like, so contrast
and palette decisions stay in the design layer and a palette change is not a data migration.

Allocated least-used-first within the **owning** organization, so a business with several
administrations gets visibly different markers. It cannot guarantee distinctness inside a **firm's**
portfolio — that spans many owning organizations, and ten colours will not cover hundreds of
clients — so the switcher reports `colour_is_ambiguous` when two entries in the same switcher share
one, and the header says so. Pretending collisions cannot happen would be worse than admitting them:
someone relying on a signal that is not distinguishing for them is exactly the failure mode.

Every badge also carries `initials`. That is the accessibility answer *and* the correctness one: a
surface too small for a full name, a monochrome rendering, or a colour-vision deficiency all leave
the client identified by something a colour cannot be confused with.

### Search: three keys, two behaviours

Names match by **substring** — a bookkeeper types the fragment they remember, not the opening of the
legal name. KvK numbers match by **prefix**: typing "1234" should find 12345678, and trigram
similarity over digit strings returns noise. An all-digit query is therefore a company-number search
and is not matched against names at all.

Ordering is exact → prefix → substring. That is not cosmetic either: keyboard reachability is
type-then-Enter, which only works if position one is predictably what the typist meant.

`trade_name` is new. FR-FRM-000 names three search keys and the schema had two; a Dutch business
commonly trades under a name that is not its statutory one, and that is the name on the invoice a
bookkeeper is looking at.

Filtering happens in the query, not in Python. "Showing only granted administrations" is a tenancy
guarantee: a switcher that fetched every administration and hid some would have leaked the names
into a response body before hiding them.

### "Keyboard-reachable" taken seriously

Not "focusable with Tab". Ctrl/Cmd+K opens from anywhere; focus lands in the search field; ↓/↑ move
the active option **wrapping at both ends**; Home/End jump; Enter selects; Escape closes. ARIA
combobox over listbox with `aria-activedescendant` rather than moving DOM focus, so typing keeps
working while the active option changes.

Wrapping matters more than it looks: without it, holding ↓ silently stops at the last entry and a
person who expected to cycle believes the list ended where it did not.

The shortcut is a hook, not a listener inside the component — the switcher is not mounted while
closed, and a component that must be rendered to hear the shortcut that opens it cannot work.

### Screen preservation needs a closed set of screens

"Preserves the current screen type **where it exists for the target client**" cannot be answered by
an open string, because the sentence turns on which screens can fail to exist. `Screen` is therefore
an enum, with `_CONDITIONAL_SCREENS` naming what each conditional one requires. Only VAT is
conditional today (an administration with no VAT registration has no VAT return to show); saying the
rest are universal *explicitly* is what makes the fallback a rule rather than a guess.

It is resolved server-side so web and mobile land in the same place from the same switch.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Treating FR-FRM-000a as presentation only — a badge and nothing else | The requirement names the harm, not the widget. A badge makes posting to the wrong client less likely and never impossible. |
| Deriving the colour from a hash of the administration id | Globally stable but collides at random, including between two clients in the same switcher — the only place a collision actually costs anything. |
| Storing a hex colour | Puts contrast decisions in the database, makes a palette change a data migration, and stops web and mobile honouring their own conventions. |
| Guaranteeing distinct colours by reassigning within the switcher | Colour would stop being stable per client, which is what makes it recognisable at all. Stability beats guaranteed distinctness when the name is authoritative anyway. |
| Answering 403 for a wrong-client write | Indistinguishable from a permission problem; sends the debugger to the grant tables for a client-state bug. |
| Applying the guard to reads too | Opening a client from a link or search result is ordinary. Refusing it protects against nothing — the named failure is posting. |
| An explicit opt-in list of routes the guard covers | Forgettable in exactly the way `AuthorizationEnforcementMiddleware` exists to prevent. Default-on with a declared opt-out inverts that. |
| Filtering the switcher client-side | The browser would already hold clients the user may not see; the leak happens before the hiding does. |
| Trigram matching for KvK numbers | Similarity over digit strings returns noise. Prefix is what someone typing a company number means. |
| An open string for the current screen | "Where it exists for the target" needs to know which screens can be absent; a string cannot answer that. |

## Consequences

- **Thirteen guards are mutation-tested** — the active-client guard removed, applied to reads,
  ignoring its opt-out, firing with no client open, and moved after authorization; KvK queries
  treated as names; blank queries treated as filters; initials falling back to ASCII-only tokenising
  and disappearing entirely; colour collisions unflagged; screen preservation always-on and
  always-off; and switching to an ungranted client resolving anyway. All produced real test failures.
- **A real bug was caught by a test, in `initials_for`.** An ASCII-only `[A-Za-z0-9]+` mangled
  accented names — "ç Ünïcode B.V." initialled as "NC" — which matters in the Dutch market this ships
  to first. Now `[^\W_]+` with Unicode. `\d` was tightened to `[0-9]` for the same class of reason:
  in Unicode mode `\d` matches Arabic-Indic digits, and a KvK number is eight ASCII ones.
- **The web app is no longer a bare scaffold.** `pnpm install` was run and the 27 new component tests
  execute for real, alongside typecheck, lint and Prettier. Web and API now both have verified
  coverage of this requirement, which was not true of any previous change here.
- **`TenantContext` gained `active_administration_id`** from an `adm` claim — the same placeholder
  mechanism as `mfa_verified` and `sid`. A real session integration reads it from
  `sessions.active_administration_id` (0017), which the switcher writes. The claim being omittable is
  the API-client case, not a bypass.
- **The mobile app does not exist**, so FR-FRM-000a's "on web and mobile" is half met. The shared
  `ClientBadge` type and the token-not-hex colour decision are what will let the mobile header be the
  same badge rather than a re-derivation.
- **`FR-FRM-000b` (grouping, tagging, saved filters) is not built** — it is S priority and not in
  this scope.
- **`0018_client_switcher.sql` has not been executed.** Parse-checked against the Postgres dialect
  only; CI applies it and exercises the colour-allocation trigger and the trigram indexes for real.
  The two new routes carry IAM-005 isolation tests, which the coverage harness demanded the moment
  they were registered.
