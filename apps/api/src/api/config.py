from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://ledgr_app@localhost:5432/ledgr"
    jwt_signing_key: str = "insecure-dev-key-change-me"

    # --- Envelope encryption (SEC-022, IAM-004, SEC-023) ---
    # "local" is dev/test only: a KEK held in an env var rather than a real
    # KMS. Production runs "gcp-kms" (ADR-062) — see
    # apps/api/src/api/crypto/kms.py. "azure-key-vault" is kept selectable
    # only until gcp-kms is live everywhere (ADR-062's own Consequences);
    # do not stand up a new deployment on it.
    kms_provider: Literal["local", "azure-key-vault", "gcp-kms"] = "local"
    local_dev_kek: str = "insecure-dev-kek-32-bytes-only!!"  # dev/test only, never production
    azure_key_vault_url: str | None = None
    azure_kek_name: str = "ledgr-tenant-dek-kek"
    # The KEK's crypto-key resource name (no version segment), e.g.
    # "projects/<project>/locations/europe-west4/keyRings/ledgr/cryptoKeys/
    # tenant-dek-kek". Authentication is Application Default Credentials
    # (a service-account JSON key via GOOGLE_APPLICATION_CREDENTIALS,
    # matching how AzureKeyVaultKeyManagementService's DefaultAzureCredential
    # reads ambient environment/managed-identity, not a value this Settings
    # class holds itself).
    gcp_kms_key_resource_name: str | None = None

    # --- Document/attachment storage (FR-DOC-001/005, IAM-004, SEC-005) ---
    # "in-memory" is dev/test only. Production runs "r2" (ADR-062,
    # superseding PRD §13's Azure Blob choice) — see
    # apps/api/src/api/documents/storage.py.
    blob_provider: Literal["in-memory", "r2"] = "in-memory"
    r2_account_id: str | None = None
    r2_bucket: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None

    # Connects as ledgr_ops (BYPASSRLS, narrowly scoped - see ADR-003 and
    # migrations/0002_encryption_keys.sql). Only used by the scheduled
    # rotation/re-wrap scripts in apps/api/scripts, never by the API itself.
    ops_database_url: str | None = None

    # --- Authentication (IAM-013, IAM-016) ---
    # "local" is dev/test only (no network breach screening) - production
    # must set "hibp". See api.auth.breach_check.build_breach_checker.
    breach_checker_provider: Literal["local", "hibp"] = "local"
    # IAM-017: "local" is dev/test only (no network, no location ever
    # returned) - see api.auth.geolocation.build_geolocation_resolver.
    geolocation_provider: Literal["local", "ip-api"] = "local"
    session_max_lifetime_hours: int = 12
    session_idle_timeout_minutes_privileged: int = 30
    # ADR-061: how long a device that already cleared IAM-011's gate once may
    # skip re-proving it. Independent of session_max_lifetime_hours - a
    # trusted device still gets a fresh, ordinary session on each login, this
    # only decides whether that new session starts pre-verified.
    trusted_device_lifetime_days: int = 7

    # --- Google sign-in (IAM-010a, IAM-010b) ---
    # No default: unset means Google sign-in is unavailable rather than
    # silently pointed at a placeholder client. See
    # api.auth.google_oidc.build_google_oidc_client. google_redirect_uri must
    # be byte-for-byte the URI registered on the OAuth client in Google Cloud
    # Console AND the page the web app reads `?code&state` from - see
    # .env.example for the contract.
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_redirect_uri: str | None = None

    # --- E-mail verification (IAM-010b) ---
    # Where links in e-mails point: the web app's origin. The verification
    # link is `{app_base_url}/verify-email?token=...`, which the web app turns
    # into POST /v1/auth/verify-email. Distinct from webauthn_origin even
    # though the two coincide locally - one is a security boundary passkeys
    # are checked against, the other is where a person is sent.
    app_base_url: str = "http://localhost:5173"
    # Development only. With email_provider=collecting nothing is ever sent;
    # this exposes what WOULD have been sent on GET /v1/dev/outbox so the
    # frontend can read a verification link without a mailbox. The route is
    # not registered at all unless this is true, and it is never true by
    # default - see api.mail.dev_outbox.
    expose_dev_outbox: bool = False

    # --- WebAuthn passkeys (IAM-010) ---
    # rp_id is the relying party identifier (typically the bare domain,
    # e.g. "ledgr.nl") - it must be a registrable domain suffix of every
    # origin passkeys are used from. webauthn_origin is the full origin
    # (scheme + host [+ port]) checked against clientDataJSON.
    webauthn_rp_id: str = "localhost"
    webauthn_rp_name: str = "LEDGR"
    webauthn_origin: str = "http://localhost:5173"

    # --- Document archive (FR-DOC, SEC-005) ---
    # SEC-005: "malware-scanned". "local" is dev/test only - it detects the
    # EICAR test file and nothing else. Production must wire a real scanner;
    # see api.documents.scanning.build_scanner, which deliberately offers no
    # "off" setting.
    malware_scanner_provider: Literal["local"] = "local"

    # --- Invoice delivery (FR-AR-005) ---
    # "collecting" is dev/test only: it assembles the message, keeps it, and
    # sends nothing. Production must set "smtp" - see
    # api.mail.sender.build_email_sender.
    #
    # PRIV-010/PRIV-011 put all customer data and every sub-processor inside
    # the EU, and an invoice e-mail carries a customer's name, address and what
    # they owe. Choosing the host below is therefore a compliance decision, not
    # an operational one, and there is deliberately no default worth having.
    email_provider: Literal["collecting", "smtp"] = "collecting"
    email_smtp_host: str | None = None
    email_smtp_port: int = 587
    email_smtp_username: str | None = None
    email_smtp_password: str | None = None
    #: Implicit TLS (port 465). False means STARTTLS, which is still upgraded
    #: before authentication - there is no plaintext option (SEC-020).
    email_smtp_use_tls: bool = False

    # The envelope sender. A customer sees the SUPPLIER's name in the From
    # line (the display name comes from the administration), but the address
    # has to be one this deployment is authorised to send from - SPF and DKIM
    # are published for our domain, not for every client's.
    email_from_address: str = "noreply@ledgr.example"

    # --- Invoice rendering (FR-TPL-017) ---
    # "minimal-pdf" is the only renderer built: one A4 layout, no templates,
    # not PDF/A-3 (FR-TPL-015) and not tagged (FR-TPL-016). The template
    # designer (FR-TPL-001..014) plugs in here behind the same Protocol, and
    # FR-TPL-017 keeps holding across that change because immutability lives in
    # the storage rather than in the renderer - see api.invoicing.rendering.
    #
    # There is deliberately no "off": an invoice issued with nothing stored is
    # one whose appearance can never afterwards be established.
    invoice_renderer_provider: Literal["minimal-pdf"] = "minimal-pdf"

    # --- Customer master (FR-AR-006, FR-ONB-003) ---
    # FR-ONB-003's "validate EU VAT numbers via VIES". "syntax-only" is
    # dev/test only: it checks the format and returns `unavailable` for
    # anything well-formed, so it can never claim a number is registered when
    # nothing consulted a register. Production must set "vies-rest" - see
    # api.customers.vies.build_vies_validator, which deliberately offers no
    # "off" setting.
    vies_provider: Literal["syntax-only", "vies-rest"] = "syntax-only"

    # FR-AR-006's "Peppol participant ID discovery". P2 (FR-AR-005), so "none"
    # is the only provider that exists and it returns `not_configured` rather
    # than pretending a customer is absent from the network. See
    # api.customers.peppol for why that distinction is load-bearing.
    peppol_directory_provider: Literal["none"] = "none"

    # --- Localisation (FR-LOC-001) ---
    # Where packages/i18n/catalogue lives. Unset resolves it automatically:
    # the copy packaged into the wheel first, then the checkout five
    # directories up - see api.i18n.catalogue._candidate_directories. This
    # exists for a deployment that mounts the catalogue somewhere else, not as
    # something a normal run needs to set.
    message_catalogue_dir: str | None = None


settings = Settings()
