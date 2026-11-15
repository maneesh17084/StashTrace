"""API request contracts (Pydantic). These validate payload shape independently of the browser.

Business rules (authority, references, assortment, idempotency) are enforced again in the service
layer, so a request that passes here can still be quarantined with an error code.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import catalog as C

TS = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
_P = {k: v.pattern for k, v in C.ID_PATTERNS.items()}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LoginIn(Strict):
    employee_id: str = Field(pattern=_P["employee"])
    password: str = Field(min_length=1, max_length=200)


class MovementIn(Strict):
    source_system: Literal["POS", "WMS", "STORE_OPS"] = "STORE_OPS"
    source_event_id: str = Field(min_length=1, max_length=80)
    movement_id: Optional[str] = Field(default=None, pattern=_P["movement"])
    movement_type: Literal["RECEIPT", "SALE", "TRANSFER_OUT", "TRANSFER_IN", "RETURN", "ADJUSTMENT", "REVERSAL"]
    store_id: str = Field(pattern=_P["store"])
    sku_id: str = Field(pattern=_P["sku"])
    quantity: int
    occurred_at: str = Field(pattern=TS)
    received_at: Optional[str] = Field(default=None, pattern=TS)
    actor_id: str = Field(pattern=_P["employee"])
    shift_id: str = Field(pattern=_P["shift"])
    source_doc_id: Optional[str] = Field(default=None, pattern=_P["doc"])
    transfer_id: Optional[str] = Field(default=None, pattern=_P["transfer"])
    reason_code: Optional[str] = None
    approved_by: Optional[str] = Field(default=None, pattern=_P["employee"])
    reverses_movement_id: Optional[str] = Field(default=None, pattern=_P["movement"])
    return_id: Optional[str] = Field(default=None, pattern=_P["return"])
    original_sale_movement_id: Optional[str] = Field(default=None, pattern=_P["movement"])
    disposition: Optional[Literal["RESTOCK", "DAMAGED", "VENDOR_RETURN", "DISPOSE"]] = None
    return_reason: Optional[str] = None
    count_ref: Optional[str] = Field(default=None, pattern=_P["count"])

    @field_validator("quantity")
    @classmethod
    def _nonzero(cls, v: int) -> int:
        if v == 0:
            raise ValueError("quantity must not be zero")
        return v

    @model_validator(mode="after")
    def _sign(self):
        if self.movement_type != "ADJUSTMENT" and self.quantity < 0:
            raise ValueError("quantity must be positive for non-adjustment movements")
        return self


class CountIn(Strict):
    count_id: Optional[str] = Field(default=None, pattern=_P["count"])
    store_id: str = Field(pattern=_P["store"])
    sku_id: str = Field(pattern=_P["sku"])
    counted_qty: int = Field(ge=0, le=1_000_000)
    counted_at: str = Field(pattern=TS)
    count_cutoff_at: Optional[str] = Field(default=None, pattern=TS)
    shift_id: str = Field(pattern=_P["shift"])
    count_type: Literal["SCHEDULED", "AD_HOC", "BLIND"] = "AD_HOC"
    counted_by: Optional[str] = Field(default=None, pattern=_P["employee"])


class CorrectionIn(Strict):
    """Fields to override on a quarantined row. Unlisted fields keep the original value."""
    store_id: Optional[str] = Field(default=None, pattern=_P["store"])
    sku_id: Optional[str] = Field(default=None, pattern=_P["sku"])
    quantity: Optional[int] = None
    reason_code: Optional[str] = None
    approved_by: Optional[str] = Field(default=None, pattern=_P["employee"])
    actor_id: Optional[str] = Field(default=None, pattern=_P["employee"])
    shift_id: Optional[str] = Field(default=None, pattern=_P["shift"])
    source_doc_id: Optional[str] = Field(default=None, pattern=_P["doc"])
    transfer_id: Optional[str] = Field(default=None, pattern=_P["transfer"])
    disposition: Optional[str] = None
    occurred_at: Optional[str] = Field(default=None, pattern=TS)
    counted_qty: Optional[int] = Field(default=None, ge=0)
    counted_by: Optional[str] = Field(default=None, pattern=_P["employee"])
    count_cutoff_at: Optional[str] = Field(default=None, pattern=TS)


class ReversalIn(Strict):
    reason: str = Field(min_length=10, max_length=500)
    replacement: Optional[MovementIn] = None


class DetectionRunIn(Strict):
    as_of: Optional[str] = Field(default=None, pattern=TS)


class InvestigationEventIn(Strict):
    event_type: Literal["ASSIGNED", "REVIEW_STARTED", "FINDING_ADDED", "RESOLVED"]
    assignee_id: Optional[str] = Field(default=None, pattern=_P["employee"])
    note: Optional[str] = Field(default=None, max_length=4000)
    resolution_code: Optional[Literal[tuple(C.RESOLUTION_CODES)]] = None  # type: ignore[valid-type]
    write_off_qty: Optional[int] = Field(default=None, gt=0, le=100000)
    expected_seq: Optional[int] = Field(default=None, ge=1)
