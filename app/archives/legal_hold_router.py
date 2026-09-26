from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.database import get_connection, transaction
from app.archives.legal_hold import DisposalPlanService, LegalHoldService, RetentionAssessmentService
from app.archives.legal_hold_schemas import (
    DisposalPlanCancel,
    DisposalPlanCreate,
    DisposalPlanExecute,
    LegalHoldCreate,
    LegalHoldExtend,
    LegalHoldReactivate,
    LegalHoldRelease,
    RetentionAssessRequest,
    RetentionSweepRequest,
)

router = APIRouter(tags=["法律保全与处置计划"])


# ---------------------------------------------------------------- 法律保全
@router.post("/api/legal-holds", status_code=status.HTTP_201_CREATED)
def create_legal_hold(payload: LegalHoldCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return LegalHoldService(connection).create(principal, payload.model_dump())


@router.get("/api/legal-holds")
def list_legal_holds(
    state: str | None = Query(default=None),
    scope_type: str | None = Query(default=None),
    principal: Principal = Depends(current_principal),
):
    return LegalHoldService(get_connection()).list(principal, state, scope_type)


@router.get("/api/legal-holds/{hold_id}")
def get_legal_hold(hold_id: int, principal: Principal = Depends(current_principal)):
    return LegalHoldService(get_connection()).get(principal, hold_id)


@router.post("/api/legal-holds/{hold_id}/extensions")
def extend_legal_hold(hold_id: int, payload: LegalHoldExtend, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return LegalHoldService(connection).extend(principal, hold_id, payload.model_dump())


@router.post("/api/legal-holds/{hold_id}/release")
def release_legal_hold(hold_id: int, payload: LegalHoldRelease, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return LegalHoldService(connection).release(principal, hold_id, payload.model_dump())


@router.post("/api/legal-holds/{hold_id}/reactivate")
def reactivate_legal_hold(hold_id: int, payload: LegalHoldReactivate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return LegalHoldService(connection).reactivate(principal, hold_id, payload.model_dump())


# ------------------------------------------------------------ 保存期限评估
@router.post("/api/retention/assessments", status_code=status.HTTP_201_CREATED)
def assess_retention(payload: RetentionAssessRequest, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return RetentionAssessmentService(connection).assess_dossier(principal, payload.dossier_id)


@router.post("/api/retention/assess-due")
def assess_due_retention(payload: RetentionSweepRequest, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return RetentionAssessmentService(connection).assess_due(principal, payload.as_of)


@router.get("/api/retention/assessments")
def list_retention_assessments(
    dossier_id: int | None = Query(default=None),
    principal: Principal = Depends(current_principal),
):
    return RetentionAssessmentService(get_connection()).list(principal, dossier_id)


# ---------------------------------------------------------------- 处置计划
@router.post("/api/disposal-plans", status_code=status.HTTP_201_CREATED)
def create_disposal_plan(payload: DisposalPlanCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return DisposalPlanService(connection).create(principal, payload.model_dump())


@router.get("/api/disposal-plans")
def list_disposal_plans(
    state: str | None = Query(default=None),
    dossier_id: int | None = Query(default=None),
    principal: Principal = Depends(current_principal),
):
    return DisposalPlanService(get_connection()).list(principal, state, dossier_id)


@router.get("/api/disposal-plans/{plan_id}")
def get_disposal_plan(plan_id: int, principal: Principal = Depends(current_principal)):
    return DisposalPlanService(get_connection()).get(principal, plan_id)


@router.post("/api/disposal-plans/{plan_id}/resume")
def resume_disposal_plan(plan_id: int, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return DisposalPlanService(connection).resume(principal, plan_id)


@router.post("/api/disposal-plans/{plan_id}/cancel")
def cancel_disposal_plan(plan_id: int, payload: DisposalPlanCancel, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return DisposalPlanService(connection).cancel(principal, plan_id, payload.model_dump())


@router.post("/api/disposal-plans/{plan_id}/execute", status_code=status.HTTP_201_CREATED)
def execute_disposal_plan(plan_id: int, payload: DisposalPlanExecute, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return DisposalPlanService(connection).execute(principal, plan_id, payload.model_dump())


@router.get("/api/disposal-plans/{plan_id}/verification")
def verify_disposal_plan(plan_id: int, principal: Principal = Depends(current_principal)):
    return DisposalPlanService(get_connection()).verification(principal, plan_id)
