from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class LegalHoldCreate(BaseModel):
    hold_code: str | None = Field(default=None, min_length=3, max_length=64)
    scope_type: Literal["project", "family", "incident"]
    scope_key: str = Field(min_length=1, max_length=100)
    source_type: Literal["litigation", "regulatory_investigation"]
    source_reference: str = Field(min_length=2, max_length=200)
    reason: str = Field(min_length=4, max_length=1000)
    review_by: str = Field(min_length=10, max_length=40)


class LegalHoldExtend(BaseModel):
    new_review_by: str = Field(min_length=10, max_length=40)
    reason: str = Field(min_length=4, max_length=1000)


class LegalHoldRelease(BaseModel):
    reason: str = Field(min_length=4, max_length=1000)


class LegalHoldReactivate(BaseModel):
    review_by: str = Field(min_length=10, max_length=40)


class RetentionAssessRequest(BaseModel):
    dossier_id: int = Field(gt=0)


class RetentionSweepRequest(BaseModel):
    as_of: str | None = Field(default=None, min_length=10, max_length=40)


class DisposalPlanCreate(BaseModel):
    plan_code: str | None = Field(default=None, min_length=3, max_length=64)
    dossier_id: int = Field(gt=0)
    approval_request_id: int = Field(gt=0)
    planned_quantity: float = Field(gt=0)
    method: str = Field(min_length=2, max_length=200)


class DisposalPlanExecute(BaseModel):
    witness_one: int = Field(gt=0)
    witness_two: int = Field(gt=0)


class DisposalPlanCancel(BaseModel):
    reason: str = Field(min_length=4, max_length=1000)
