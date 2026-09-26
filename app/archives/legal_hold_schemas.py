from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


def _require_iso_date(value: str, field: str) -> str:
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field}必须是 ISO-8601 日期或时间") from exc
    return value


class LegalHoldScope(BaseModel):
    type: Literal["project", "family", "incident"]
    key: str = Field(min_length=1, max_length=100)


class LegalHoldCreate(BaseModel):
    hold_code: str | None = Field(default=None, max_length=64)
    matter_reference: str = Field(min_length=2, max_length=100)
    notice_type: Literal["litigation", "regulatory_investigation"]
    reason: str = Field(min_length=4, max_length=1000)
    scope: LegalHoldScope
    review_until: str = Field(min_length=8, max_length=40)

    @field_validator("review_until")
    @classmethod
    def check_review_until(cls, value: str) -> str:
        return _require_iso_date(value, "复核期限")


class LegalHoldExtend(BaseModel):
    new_review_until: str = Field(min_length=8, max_length=40)
    reason: str = Field(min_length=2, max_length=500)

    @field_validator("new_review_until")
    @classmethod
    def check_new_review_until(cls, value: str) -> str:
        return _require_iso_date(value, "新的复核期限")


class LegalHoldRelease(BaseModel):
    reason: str = Field(min_length=2, max_length=500)


class DisposalPlanCreate(BaseModel):
    plan_code: str | None = Field(default=None, max_length=64)
    dossier_id: int = Field(gt=0)
    approval_request_id: int = Field(gt=0)


class DisposalPlanCancel(BaseModel):
    reason: str = Field(min_length=2, max_length=500)
