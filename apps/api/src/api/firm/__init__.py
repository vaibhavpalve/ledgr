"""Firm-facing capability (PRD §5.2, FR-FRM-*).

The client switcher lives here rather than in api.authz because it is a
product surface rather than an access decision - what a firm user sees and
how they move between clients. It READS the authorization model (only
granted administrations appear) but decides nothing about access.

The one part of FR-FRM-000a that IS an access decision - refusing a write
aimed at a different client from the one the session has open - lives in
api.authz.dependencies with the rest of the request-time checks, so it
cannot be forgotten by a route that forgets to import this package.
"""
