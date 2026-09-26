from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import current_principal
from app.database import get_connection, transaction
from app.core.security import Principal
from app.archives.extended_schemas import DisposalExecute
from app.archives.legal_hold import DisposalPlanService, LegalHoldService, RetentionEvaluationService
from app.archives.legal_hold_schemas import (
    DisposalPlanCancel,
    DisposalPlanCreate,
    LegalHoldCreate,
    LegalHoldExtend,
    LegalHoldRelease,
)

hold_router = APIRouter(prefix="/api/legal-holds", tags=["法律保全"])
plan_router = APIRouter(prefix="/api/disposal-plans", tags=["处置计划"])


@hold_router.post("", status_code=status.HTTP_201_CREATED)
def create_hold(payload: LegalHoldCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return LegalHoldService(connection).create(principal, payload.model_dump())


@hold_router.get("")
def list_holds(
    status_filter: str | None = Query(default=None, alias="status"),
    principal: Principal = Depends(current_principal),
):
    return LegalHoldService(get_connection()).list(principal, status_filter)


@hold_router.get("/retention-evaluation")
def retention_evaluation(
    as_of: str | None = Query(default=None),
    principal: Principal = Depends(current_principal),
):
    return RetentionEvaluationService(get_connection()).evaluate(principal, as_of)


@hold_router.get("/by-dossier/{dossier_id}")
def holds_by_dossier(dossier_id: int, principal: Principal = Depends(current_principal)):
    return LegalHoldService(get_connection()).for_dossier(principal, dossier_id)


@hold_router.get("/{hold_id}")
def hold_detail(hold_id: int, principal: Principal = Depends(current_principal)):
    return LegalHoldService(get_connection()).detail(principal, hold_id)


@hold_router.post("/{hold_id}/extensions", status_code=status.HTTP_201_CREATED)
def extend_hold(hold_id: int, payload: LegalHoldExtend, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return LegalHoldService(connection).extend(principal, hold_id, payload.model_dump())


@hold_router.post("/{hold_id}/release")
def release_hold(hold_id: int, payload: LegalHoldRelease, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return LegalHoldService(connection).release(principal, hold_id, payload.model_dump())


@plan_router.post("", status_code=status.HTTP_201_CREATED)
def create_plan(payload: DisposalPlanCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return DisposalPlanService(connection).create(principal, payload.model_dump())


@plan_router.get("")
def list_plans(
    state: str | None = Query(default=None),
    dossier_id: int | None = Query(default=None),
    principal: Principal = Depends(current_principal),
):
    with transaction(immediate=True) as connection:
        return DisposalPlanService(connection).list(principal, state, dossier_id)


@plan_router.get("/{plan_id}")
def plan_detail(plan_id: int, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return DisposalPlanService(connection).detail(principal, plan_id)


@plan_router.post("/{plan_id}/execute")
def execute_plan(plan_id: int, payload: DisposalExecute, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return DisposalPlanService(connection).execute(principal, plan_id, payload.model_dump())


@plan_router.post("/{plan_id}/cancel")
def cancel_plan(plan_id: int, payload: DisposalPlanCancel, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return DisposalPlanService(connection).cancel(principal, plan_id, payload.model_dump())


@plan_router.get("/{plan_id}/verification")
def plan_verification(plan_id: int, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return DisposalPlanService(connection).verification(principal, plan_id)
