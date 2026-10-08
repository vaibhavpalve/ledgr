# ADR-108: The PSD2 bank feed, behind the adapter, through the statement import's own write path

- **Status**: Accepted (built; off until a provider is configured)
- **Date**: 2026-10-08
- **Builds on**: ADR-084 (bank accounts), ADR-091 (statement formats); fills the slot `api.bank.adapters` held since 0065

## Context

FR-BNK-001 asks for a daily bank feed. Until now bank lines came only from statement files a
person exports (ADR-091), and `api.bank.adapters` held a `BankFeedProvider` protocol with an
"unconfigured" implementation and nothing behind it. A live feed means PSD2 account information:
the bank's own consent page, a licensed account-information service provider (AISP) between the
bank and Boeklite, consents that end after at most 180 days, and a sync that runs without a person.

Three properties had to survive: the ledger's narrow API (non-negotiable #1), tenant isolation for
a job that touches every tenant (#3, IAM-001-005), and a replaceable provider (#4).

## Decision

- **GoCardless Bank Account Data** (the former Nordigen, EU-licensed AISP) is the first provider,
  behind the existing protocol, extended with the consent flow (`list_institutions`,
  `start_consent`, `consent_status`, `account_iban`, `fetch_transactions`, `revoke`). It is
  selected by `BANK_FEED_PROVIDER=gocardless` with `GOCARDLESS_SECRET_ID`/`_KEY`; without both
  secrets it is treated as `none`. Tink or Enable Banking is a new class in `adapters.py` and a
  configuration value; nothing else changes.
- **One write path.** Fetched lines go through `BankService.import_rows`, the same insert and
  content-hash `external_id` the statement import uses, with a `bank_statement_import` row of
  source `psd2` per sync. A line fetched by the feed and the same line in a file uploaded later
  de-duplicate on 0065's own unique index, so the two sources can be mixed without booking a
  payment twice. Nothing here posts: a fetched line is an unmatched `bank_transaction`.
- **One table, `bank_feed_connection` (0077)**: one consent per bank account (pending or linked at
  most once), with RLS like every bank table, no DELETE grant (a consent's history is evidence),
  and a trigger that keeps a connection on its own account and administration. The provider's
  consent id is not a credential; the secret that uses it is configuration only.
- **The account is chosen by IBAN.** A bank may consent several accounts; the one whose IBAN is the
  bank account's is read. Without an IBAN on our side only a single consented account is taken.
- **Outcomes are recorded, not thrown.** A refused consent, an IBAN mismatch, an expired consent
  or a provider outage is written to the connection (`status`, `last_error`) and returned with 200,
  because a raised error would roll the request's transaction back and lose that state. Only
  refusals that change nothing (no provider, nothing linked, unknown connection) are 4xx.
- **No new permissions.** Connect, complete and disconnect ride the matrix's existing "Connect /
  revoke bank consent" (`manage bank_consent`); sync takes the import's `reconcile
bank_transaction`; reads take `view bank_transaction`. The generated 0010 is untouched.
- **The daily sync runs as two roles.** `scripts/sync_bank_feeds.py` (`api.bank.feed_job`) reads
  which connections are due as `ledgr_ops` (BYPASSRLS, SELECT-only here), then syncs each one as
  `ledgr_app` in its own transaction with `app.current_org_id` set to that connection's
  organization, through the same service a "Fetch now" click uses. It writes under RLS like a
  request and cannot write into another tenant. One connection's failure does not stop the rest.
- **The screen** shows the feed only where a provider is configured: "Connect your bank" opens a
  bank picker (initials, not logos: CSP `img-src 'self'`), the consent is a full navigation to the
  bank's own page, the bank returns to `/bank/feed-return?ref=<connection>`, and Bank then shows
  "Live from your bank", the last fetch, how long access lasts, "Fetch now" and "Disconnect".

## Alternatives considered

| Option                                          | Rejected because                                                                                                   |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| The provider's transaction id as `external_id`  | Lines from the feed and from a file would never de-duplicate against each other; mixing sources would double-book. |
| A separate write path for fetched lines         | A second insert into `bank_transaction` with its own rules, the thing non-negotiable #1's spirit forbids.          |
| Running the daily sync entirely as `ledgr_ops`  | `ledgr_ops` bypasses RLS; a job writing tenant data that way has no second line of defence.                        |
| Raising errors for a refused or expired consent | The rollback would lose the very state the screen needs to explain it.                                             |
| Showing bank logos from the provider            | Needs `img-src` opened to a third party for decoration.                                                            |

## Consequences

- **Off by default.** Until `BANK_FEED_PROVIDER` and the secrets are set in an environment, the
  screen and the API behave as before and the site keeps the feed under "On the way".
- **To switch it on**: a GoCardless Bank Account Data account (check it is still accepting new
  customers; otherwise add another AISP behind the protocol), the two secrets as environment
  variables, migration 0077 applied, and a daily cron service running `scripts/sync_bank_feeds.py`
  with both `DATABASE_URL` and `OPS_DATABASE_URL`. The provider's redirect target is
  `{APP_BASE_URL}/bank/feed-return`.
- Content-hash de-duplication carries ADR-084's known limit: two genuinely identical lines (same
  day, amount, counterparty IBAN and description) on one account collapse into one.
- Consents end (90 days by default, `BANK_FEED_CONSENT_DAYS`). The connection then shows why and
  offers "Connect again"; a reminder e-mail before expiry is a follow-up.
- Only booked lines are imported; pending ones change and are read once they book.
