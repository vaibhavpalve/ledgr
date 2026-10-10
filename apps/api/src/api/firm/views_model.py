"""Saved views on the firm work queue: FR-FRM-000b, contract-wave2 decision 6 (ADR-115).

A saved view is a name on a worklist query. What it stores is exactly the parameters
GET /v1/firm/worklist takes - `chip`, `q`, `assigned`, `sort`, `dir`, `vat_frequency` - and
nothing derived. Its count is computed at read time by `api.firm.worklist_model.count_rows` over
the same rows the worklist shows, so "Q3 VAT (12)" opens on twelve clients.

`assigned` is stored as written: `me` stays `me` and resolves to whoever reads the view (always its
owner - views are per user).
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.firm.worklist_model import (
    AssignedFilter,
    Chip,
    SortKey,
    VatFrequency,
    WorklistQuery,
)

#: Contract-wave2: at most this many active (unarchived) views per person.
MAX_ACTIVE_VIEWS = 20
NAME_MAX_LENGTH = 60


class SavedViewQuery(BaseModel):
    """The stored query. Unknown keys are refused, so a typo is a 422 and not a view that
    silently ignores half of what was asked."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    chip: Chip = Chip.MY_MOVE
    q: str | None = Field(default=None, max_length=200)
    assigned: str = "any"
    sort: SortKey = SortKey.RISK
    dir: Literal["asc", "desc"] = "asc"
    vat_frequency: VatFrequency | None = None

    @field_validator("q")
    @classmethod
    def _blank_search_is_none(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return value.strip()

    @field_validator("assigned")
    @classmethod
    def _assigned_is_me_any_or_a_user(cls, value: str) -> str:
        if value in ("me", "any"):
            return value
        try:
            return str(uuid.UUID(value))
        except ValueError as exc:
            raise ValueError("assigned must be 'me', 'any' or a user id") from exc

    def stored(self) -> dict[str, object]:
        """The jsonb written to saved_view.query (enums as their wire values)."""
        return self.model_dump(mode="json")

    def to_worklist_query(self, *, user_id: uuid.UUID) -> WorklistQuery:
        return WorklistQuery(
            chip=self.chip,
            q=self.q,
            assigned=AssignedFilter.parse(self.assigned, user_id=user_id),
            sort=self.sort,
            descending=self.dir == "desc",
            vat_frequency=self.vat_frequency,
        )


def parse_stored_query(raw: object) -> SavedViewQuery | None:
    """A stored query read back. None if it no longer validates (a chip or sort key retired
    since it was saved): the view is still listed, with no count, rather than failing the list."""
    if not isinstance(raw, dict):
        return None
    try:
        return SavedViewQuery.model_validate(raw)
    except ValueError:
        return None


def clean_name(raw: str) -> str | None:
    """1-60 characters after trimming; None when it is not."""
    name = raw.strip()
    if not name or len(name) > NAME_MAX_LENGTH:
        return None
    return name
