"""Try to issue an invoice inside a savepoint and say why not, if not - shared by the callers
that issue as a side effect of something else (a recurring schedule, a batch).

A refusal is not an error for them: the invoice stays a DRAFT for a person to finish, and the
gapless number series is untouched because the allocation rolls back with the savepoint
(FR-AR-004). The reason is a stable code the caller reports and a screen can translate; the
sentences live in the catalogue as `invoice.recurring.issue_error.<code>`.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from api.invoicing.approval import ApprovalRequired
from api.invoicing.model import NotAuthorizedToInvoice, NotStatutoryCompliant
from api.invoicing.posting import NoOpenPeriod, NoSalesJournal, PostingConfigurationMissing
from api.invoicing.service import InvoicingService
from api.templates.assets import LogoNotRenderable

__all__ = ["attempt_issue"]


async def attempt_issue(
    *,
    invoicing: InvoicingService,
    savepoint: Callable[[], AbstractAsyncContextManager[Any]],
    administration_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    invoice_id: uuid.UUID,
) -> tuple[bool, str | None]:
    """`(True, None)` when issued; `(False, code)` when it was refused and left a draft."""
    try:
        async with savepoint():
            await invoicing.issue(
                administration_id=administration_id,
                invoice_id=invoice_id,
                actor_user_id=actor_user_id,
            )
    except NotAuthorizedToInvoice:
        return False, "not_authorized_to_issue"
    except ApprovalRequired:
        # SI-16: the administration requires the owner's approval and this draft has none.
        return False, "approval_required"
    except NotStatutoryCompliant:
        return False, "not_statutory_compliant"
    except NoOpenPeriod:
        return False, "no_open_period"
    except NoSalesJournal:
        return False, "no_sales_journal"
    except PostingConfigurationMissing:
        return False, "posting_unconfigured"
    except LogoNotRenderable:
        return False, "template_not_renderable"
    return True, None
