.PHONY: dev dev-up dev-down test test-web test-api test-api-db-setup test-api-isolation lint lint-web lint-api format format-web format-api typecheck typecheck-web typecheck-api install scan scan-web scan-api secret-scan generate-role-catalogue check-role-catalogue verify-ledger-integrity enforce-document-retention load-rgs check-rgs load-vat check-vat check-i18n list-i18n review-glossary review-invoice-wording

# Bring up local infra (Postgres, Azurite) and start the web dev server.
dev: dev-up
	pnpm run dev:web

dev-up:
	docker compose up -d

dev-down:
	docker compose down

install:
	pnpm install --frozen-lockfile
	cd apps/api && uv sync

test: test-web test-api

test-web:
	pnpm run test

test-api:
	cd apps/api && uv run pytest

# One-time (or after a schema change) setup of a local Postgres for the
# IAM-005 tenant-isolation suite. Requires TEST_DATABASE_ADMIN_URL and
# TEST_LEDGR_APP_PASSWORD in .env — see .env.example.
test-api-db-setup:
	cd apps/api && uv run python scripts/bootstrap_test_db.py

# Runs the full suite with the DB-backed isolation tests actually executing
# (not skipped). Requires test-api-db-setup to have been run, and
# DATABASE_URL in .env pointed at that database as ledgr_app.
test-api-isolation:
	cd apps/api && TENANT_ISOLATION_TESTS_ENABLED=1 uv run pytest

# Regenerates migrations/0010_role_catalogue.sql from PRD Appendix A and §8.4
# as encoded in apps/api/src/api/authz/matrix.py. Run this after changing the
# matrix; the generated migration is checked in.
generate-role-catalogue:
	cd apps/api && uv run python scripts/generate_role_catalogue.py

# Fails if the checked-in catalogue migration is stale. `make test-api` also
# covers this (tests/authz/test_role_catalogue_generation.py); this target
# exists so the check can be run on its own without the suite.
check-role-catalogue:
	cd apps/api && uv run python scripts/generate_role_catalogue.py --check

# NFR-033: verifies debit/credit balance, sub-ledger to control account
# agreement and numbering continuity, and alerts on any deviation. The same
# command the nightly schedule runs, so the scheduled path is not one nobody
# has ever watched. Requires OPS_DATABASE_URL (ledgr_ops); pass
# ARGS="--app-connection --organization <uuid>" to check one tenant through
# the application role instead.
#
# Exits 0 intact, 1 deviations found, 2 could not run, 3 nothing examined -
# so it can be used as a gate, and as a test oracle.
verify-ledger-integrity:
	cd apps/api && uv run python scripts/verify_ledger_integrity.py $(ARGS)

# PRIV-030: sweeps documents whose FR-DOC-002 retention has run out and
# removes them, reporting an exception for any that could not be (FR-DOC-003
# links still naming them). Exits 0 clean, 1 exceptions found, 2 could not run.
enforce-document-retention:
	cd apps/api && uv run python scripts/enforce_document_retention.py $(ARGS)

# CMP-003: loads an RGS reference dataset. Supporting a new RGS release is
# running this against a new file - no migration, no deploy. Requires
# OPS_DATABASE_URL (ledgr_ops), which is the only role granted
# ledger.load_rgs_version.
#
# --allow-provisional is required by the shipped dataset, which is a starter
# subset rather than the official publication; see the file's own _readme.
load-rgs:
	cd apps/api && uv run python scripts/load_rgs_version.py --allow-provisional --publish $(ARGS)

# Validates the dataset without touching a database. `make test-api` covers
# this too (tests/ledger/test_rgs_dataset.py); this target exists so a file
# being edited by hand can be checked on its own.
check-rgs:
	cd apps/api && uv run python scripts/load_rgs_version.py --check $(ARGS)

# CMP-014: loads an effective-dated VAT ruleset. A rate change is a new file,
# not a migration. Requires OPS_DATABASE_URL (ledgr_ops), the only role granted
# vat.load_ruleset.
#
# A rule may only take effect AFTER the last day any period has been VAT-filed
# through, so a rate change has to be loaded before that period is filed -
# which is CMP-013's lead time, enforced rather than scheduled. A refusal means
# the window has closed: the route from there is a suppletie (FR-VAT-005), not
# a rate edit.
load-vat:
	cd apps/api && uv run python scripts/load_vat_rules.py --allow-provisional $(ARGS)

# Validates a ruleset without touching a database. `make test-api` covers this
# too (tests/vat/); this target exists so a file being edited by hand can be
# checked on its own.
check-vat:
	cd apps/api && uv run python scripts/load_vat_rules.py --check $(ARGS)

# FR-LOC-001d: "Translation completeness is enforced in CI: a build fails if
# any user-facing string lacks a translation in either language."
#
# Both runtimes RAISE on a message they cannot resolve rather than falling back
# from Dutch to English (FR-LOC-001 makes that a release blocker, not a
# fallback) - which is only a defensible posture because this runs first.
#
# Stdlib Python, no database, no node_modules: it runs anywhere, which is what
# lets it be the first thing CI does rather than something gated behind a
# working install.
check-i18n:
	python3 scripts/check_translations.py

# Every key with both languages, for reading or diffing.
list-i18n:
	python3 scripts/check_translations.py --list

# FR-AR-002: "Correct legal wording rendered per treatment." Prints the eight
# VAT treatments' invoice statements as a sheet for a Dutch tax adviser to sign
# off — least settled first, each with its statutory basis and what is still
# open. Nothing in this system can check that the words are the right ones; this
# is the page that lets somebody who can.
#
# A sheet, not a gate. It never exits non-zero on unreviewed wording — see the
# script's own docstring, and check_translations.py's argument about the
# glossary.
review-invoice-wording:
	cd apps/api && uv run python scripts/review_invoice_wording.py

# FR-LOC-001c: "Dutch accounting terminology is authoritative and reviewed by a
# practising Dutch accountant." Prints packages/i18n/catalogue/glossary.json as
# a review sheet — least settled terms first, each with what it still needs
# decided. Rendered from the glossary rather than kept as a second document, so
# the sheet that gets signed off is the terminology that ships.
review-glossary:
	python3 scripts/check_translations.py --glossary

lint: lint-web lint-api check-i18n

lint-web:
	pnpm run lint

lint-api:
	cd apps/api && uv run ruff check .

format: format-web format-api

format-web:
	pnpm run format

format-api:
	cd apps/api && uv run ruff format .

typecheck: typecheck-web typecheck-api

typecheck-web:
	pnpm run typecheck

typecheck-api:
	cd apps/api && uv run mypy src

# SEC-051: SCA scanning. Mirrors what CI enforces (fails on high/critical).
scan: scan-web scan-api

scan-web:
	pnpm audit --audit-level=high

scan-api:
	cd apps/api && uv run pip-audit --strict

secret-scan:
	docker run --rm -v "$$(pwd):/repo" zricethezav/gitleaks:latest detect --source=/repo --no-git -v
