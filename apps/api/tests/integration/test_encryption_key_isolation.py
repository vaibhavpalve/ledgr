"""DB-backed proof that administration_encryption_key genuinely enforces
tenant isolation via RLS (IAM-002), and that the extended
app.create_firm_client_administration (migration 0002) atomically seeds a
key row for the firm-bootstrap path. Complements
tests/crypto/test_envelope_encryption.py, which proves the encryption LOGIC
in isolation from the database.

Skips without a live Postgres - see tests/integration/conftest.py.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from api.crypto.envelope import EnvelopeEncryptionService
from api.crypto.kms import LocalDevKeyManagementService
from api.crypto.repository import SqlAdministrationKeyRepository
from api.db import engine as app_engine
from tests.support.seed import seed_administration, signup_organization

_TEST_KMS = LocalDevKeyManagementService(
    current_kek_id="test-kek-v1", keks={"test-kek-v1": b"9" * 32}
)


async def test_org_a_cannot_see_org_bs_wrapped_key() -> None:
    org_a = await signup_organization(app_engine, name="Isolation Test Co", kvk="33333333")
    org_b = await signup_organization(app_engine, name="Isolation Test Co", kvk="44444444")
    admin_a = await seed_administration(
        app_engine, org_id=org_a, legal_name="Isolation Test Co", legal_form="BV"
    )
    admin_b = await seed_administration(
        app_engine, org_id=org_b, legal_name="Isolation Test Co", legal_form="BV"
    )

    session_factory = async_sessionmaker(app_engine, expire_on_commit=False)

    for administration_id, org_id in [(admin_a, org_a), (admin_b, org_b)]:
        async with session_factory() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.current_org_id', :org_id, true)"),
                {"org_id": str(org_id)},
            )
            repository = SqlAdministrationKeyRepository(session)
            service = EnvelopeEncryptionService(_TEST_KMS, repository)
            await service.provision_key(administration_id)

    # As org A, try to read org B's key row directly - RLS must filter it
    # out of the result set entirely, not merely reject the query.
    async with session_factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_org_id', :org_id, true)"),
            {"org_id": str(org_a)},
        )
        result = await session.execute(
            text(
                "SELECT id FROM administration_encryption_key "
                "WHERE administration_id = :administration_id"
            ),
            {"administration_id": str(admin_b)},
        )
        assert result.first() is None

        # Sanity: org A can see its OWN key, on the same connection/context.
        own = await session.execute(
            text(
                "SELECT id FROM administration_encryption_key "
                "WHERE administration_id = :administration_id"
            ),
            {"administration_id": str(admin_a)},
        )
        assert own.first() is not None


async def test_firm_bootstrap_provisions_key_atomically() -> None:
    firm_org = await signup_organization(app_engine, name="Isolation Firm", kvk="55555555")
    session_factory = async_sessionmaker(app_engine, expire_on_commit=False)

    # signup_self_managed_organization always creates kind='business'; flip
    # this one to a firm so it can call create_firm_client_administration.
    async with session_factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_org_id', :org_id, true)"),
            {"org_id": str(firm_org)},
        )
        await session.execute(
            text("UPDATE organization SET kind = 'firm' WHERE id = :id"), {"id": str(firm_org)}
        )

    dek = b"\x00" * 32
    wrapped = await _TEST_KMS.wrap_key(dek)

    async with session_factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_org_id', :org_id, true)"),
            {"org_id": str(firm_org)},
        )
        result = await session.execute(
            text(
                "SELECT id FROM app.create_firm_client_administration("
                "  :legal_name, :legal_form, :kvk, :vat, :invited_by, "
                "  :wrapped_dek, :wrap_algorithm, :kek_key_id"
                ")"
            ),
            {
                "legal_name": "New Firm Client",
                "legal_form": "BV",
                "kvk": "66666666",
                "vat": None,
                "invited_by": None,
                "wrapped_dek": wrapped.ciphertext,
                "wrap_algorithm": wrapped.algorithm,
                "kek_key_id": wrapped.kek_key_id,
            },
        )
        new_admin_id = result.scalar_one()

        key_result = await session.execute(
            text(
                "SELECT status, key_version FROM administration_encryption_key "
                "WHERE administration_id = :administration_id"
            ),
            {"administration_id": str(new_admin_id)},
        )
        row = key_result.one()
        assert row.status == "active"
        assert row.key_version == 1
