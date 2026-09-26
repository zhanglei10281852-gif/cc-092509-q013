"""法律保全、保存期限到期评估与处置计划。

法务收到诉讼或监管调查通知后，可按项目、家族或事件冻结一组档案：
处于生效保全中的档案即使原定保存期限到期也不会进入处置；解除保全后，
只有满足密级与双人复核条件的载体才能生成处置计划。处置计划在生成时
固化当时的资格判断，保全重新生效或档案版本变化会自动暂停计划，执行后
可核对实际载体、见证人与证明摘要，历史计划与决定只追加、不覆盖。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import Principal
from app.archives.operations import DisposalService
from app.archives.repository import (
    ApprovalRepository,
    DossierRepository,
    LegalHoldRepository,
    auto_pause_plans,
    row_dict,
)
from app.archives.validation import require_code, require_iso_moment
from app.services.audit import AuditService

# 密级处置门槛：绝密载体必须先降密，不能直接生成处置计划。
NON_DISPOSABLE_SECRECY_LEVELS = {"top_secret"}

# 处置计划只允许处于这些生命周期状态的档案进入。
PLAN_ALLOWED_STATES = {"available", "partially_disclosed", "access_loaned", "received", "quarantined"}


class LegalHoldService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.holds = LegalHoldRepository(connection)
        self.dossiers = DossierRepository(connection)
        self.audit = AuditService(connection, self.clock)

    # ------------------------------------------------------------------ 范围解析
    def resolve_scope(self, scope_type: str, scope_key: str) -> list[dict[str, Any]]:
        """按项目、家族或事件解析冻结范围内的档案。"""
        if scope_type == "project":
            rows = self.connection.execute(
                """SELECT s.* FROM dossiers s JOIN intake_batches b ON b.id=s.intake_id
                   WHERE b.project_code=? ORDER BY s.id""",
                (scope_key,),
            ).fetchall()
        elif scope_type == "family":
            root = self.connection.execute(
                "SELECT id FROM dossiers WHERE dossier_code=?", (scope_key,)
            ).fetchone()
            if root is None:
                raise NotFoundError("家族根档案不存在")
            rows = self.connection.execute(
                "SELECT * FROM dossiers WHERE root_dossier_id=? OR id=? ORDER BY id",
                (root[0], root[0]),
            ).fetchall()
        elif scope_type == "incident":
            case = self.connection.execute(
                "SELECT * FROM incident_cases WHERE case_code=?", (scope_key,)
            ).fetchone()
            if case is None:
                raise NotFoundError("泄密事件不存在")
            case = dict(case)
            if case.get("dossier_id"):
                rows = self.connection.execute(
                    "SELECT * FROM dossiers WHERE id=? ORDER BY id", (case["dossier_id"],)
                ).fetchall()
            elif case.get("intake_id"):
                rows = self.connection.execute(
                    "SELECT * FROM dossiers WHERE intake_id=? ORDER BY id", (case["intake_id"],)
                ).fetchall()
            else:
                rows = []
        else:
            raise ValidationError("冻结范围类型必须是 project、family 或 incident")
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ 查询
    def get(self, principal: Principal, hold_id: int) -> dict[str, Any]:
        principal.require("dossiers.read")
        return self._detail(self.holds.get(hold_id))

    def list(self, principal: Principal, state: str | None, scope_type: str | None) -> list[dict[str, Any]]:
        principal.require("dossiers.read")
        return [self._summary(item) for item in self.holds.list(state=state, scope_type=scope_type)]

    def _summary(self, hold: dict[str, Any]) -> dict[str, Any]:
        item_count = self.connection.execute(
            "SELECT COUNT(*) FROM legal_hold_items WHERE hold_id=?", (hold["id"],)
        ).fetchone()[0]
        extension_count = self.connection.execute(
            "SELECT COUNT(*) FROM legal_hold_extensions WHERE hold_id=?", (hold["id"],)
        ).fetchone()[0]
        return {**hold, "item_count": item_count, "extension_count": extension_count}

    def _detail(self, hold: dict[str, Any]) -> dict[str, Any]:
        detail = self._summary(hold)
        detail["items"] = self.holds.items(hold["id"])
        detail["extensions"] = self.holds.extensions(hold["id"])
        detail["events"] = self.holds.events(hold["id"])
        return detail

    # ------------------------------------------------------------------ 冻结
    def create(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("legal_holds.manage")
        hold_code = require_code(data["hold_code"], "保全编号") if data.get("hold_code") else f"LH-{uuid.uuid4().hex[:12].upper()}"
        review_by = require_iso_moment(data["review_by"], "保全复核期限")
        payload = {
            "scope_type": data["scope_type"],
            "scope_key": data["scope_key"].strip(),
            "source_type": data["source_type"],
            "source_reference": data["source_reference"].strip(),
            "reason": data["reason"].strip(),
        }
        existing = self.holds.by_code(hold_code)
        if existing:
            same = all(existing[key] == value for key, value in payload.items()) and existing["review_by"] == review_by
            if not same:
                raise ConflictError("保全编号已被不同的保全请求占用")
            return {"hold": self._detail(existing), "attached": [], "skipped_already_held": [], "replayed": True}
        now = to_storage(self.clock.now())
        dossiers = self.resolve_scope(payload["scope_type"], payload["scope_key"])
        active_map = self.holds.active_hold_ids_for([item["id"] for item in dossiers])
        to_attach = [item for item in dossiers if item["id"] not in active_map]
        skipped = [
            {"dossier_id": item["id"], "dossier_code": item["dossier_code"], "active_hold_ids": active_map[item["id"]]}
            for item in dossiers
            if item["id"] in active_map
        ]
        hold = self.holds.create({**payload, "hold_code": hold_code, "review_by": review_by}, principal.user_id, now)
        self.holds.attach_items(hold["id"], [item["id"] for item in to_attach], now)
        self.holds.append_event(
            hold["id"],
            "created",
            principal.user_id,
            now,
            {
                "scope_type": payload["scope_type"],
                "scope_key": payload["scope_key"],
                "source_type": payload["source_type"],
                "source_reference": payload["source_reference"],
                "attached_count": len(to_attach),
                "skipped_already_held": len(skipped),
            },
        )
        paused = self._pause_plans_for([item["id"] for item in to_attach], now, principal.user_id, hold)
        if paused:
            self.holds.append_event(hold["id"], "plans_auto_paused", principal.user_id, now, {"plan_ids": paused})
        self.audit.record(principal, "legal_hold.create", "legal_hold", str(hold["id"]), after=hold)
        return {
            "hold": self._detail(hold),
            "attached": [item["dossier_code"] for item in to_attach],
            "skipped_already_held": skipped,
            "replayed": False,
        }

    def _pause_plans_for(self, dossier_ids: list[int], now: str, actor_user_id: int, hold: dict[str, Any]) -> list[int]:
        paused: list[int] = []
        for dossier_id in dossier_ids:
            paused.extend(
                auto_pause_plans(
                    self.connection,
                    dossier_id,
                    "legal_hold_activated",
                    now,
                    actor_user_id=actor_user_id,
                    details={"hold_id": hold["id"], "hold_code": hold["hold_code"]},
                )
            )
        return paused

    # ------------------------------------------------------------------ 延期
    def extend(self, principal: Principal, hold_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("legal_holds.manage")
        hold = self.holds.get(hold_id)
        if hold["state"] != "active":
            raise ConflictError("只有生效中的保全可以延期")
        new_review_by = require_iso_moment(data["new_review_by"], "新的保全复核期限")
        if from_storage(new_review_by) <= from_storage(hold["review_by"]):
            raise ValidationError("新的保全复核期限必须晚于当前期限")
        now = to_storage(self.clock.now())
        self.connection.execute(
            """INSERT INTO legal_hold_extensions(hold_id,previous_review_by,new_review_by,reason,created_by,created_at)
               VALUES(?,?,?,?,?,?)""",
            (hold_id, hold["review_by"], new_review_by, data["reason"].strip(), principal.user_id, now),
        )
        self.connection.execute(
            "UPDATE legal_holds SET review_by=?,version=version+1,updated_at=? WHERE id=?",
            (new_review_by, now, hold_id),
        )
        self.holds.append_event(
            hold_id,
            "extended",
            principal.user_id,
            now,
            {"previous_review_by": hold["review_by"], "new_review_by": new_review_by, "reason": data["reason"].strip()},
        )
        updated = self.holds.get(hold_id)
        self.audit.record(principal, "legal_hold.extend", "legal_hold", str(hold_id), before=hold, after=updated)
        return self._detail(updated)

    # ------------------------------------------------------------------ 解除
    def release(self, principal: Principal, hold_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("legal_holds.manage")
        hold = self.holds.get(hold_id)
        if hold["state"] != "active":
            raise ConflictError("保全已解除，不能重复操作")
        now = to_storage(self.clock.now())
        self.connection.execute(
            """UPDATE legal_holds SET state='released',released_by=?,released_at=?,release_reason=?,
               version=version+1,updated_at=? WHERE id=?""",
            (principal.user_id, now, data["reason"].strip(), now, hold_id),
        )
        self.holds.append_event(hold_id, "released", principal.user_id, now, {"reason": data["reason"].strip()})
        updated = self.holds.get(hold_id)
        self.audit.record(principal, "legal_hold.release", "legal_hold", str(hold_id), before=hold, after=updated)
        return self._detail(updated)

    # ------------------------------------------------------------------ 重新生效
    def reactivate(self, principal: Principal, hold_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("legal_holds.manage")
        hold = self.holds.get(hold_id)
        if hold["state"] != "released":
            raise ConflictError("只有已解除的保全可以重新生效")
        review_by = require_iso_moment(data["review_by"], "保全复核期限")
        now = to_storage(self.clock.now())
        # 重新解析范围：冻结期间新增的档案一并纳入，仍被其他生效保全覆盖的跳过。
        dossiers = self.resolve_scope(hold["scope_type"], hold["scope_key"])
        existing_items = {item["dossier_id"] for item in self.holds.items(hold_id)}
        active_map = self.holds.active_hold_ids_for([item["id"] for item in dossiers])
        to_attach = [
            item["id"]
            for item in dossiers
            if item["id"] not in existing_items and item["id"] not in active_map
        ]
        self.holds.attach_items(hold_id, to_attach, now)
        self.connection.execute(
            """UPDATE legal_holds SET state='active',review_by=?,released_by=NULL,released_at=NULL,
               release_reason=NULL,version=version+1,updated_at=? WHERE id=?""",
            (review_by, now, hold_id),
        )
        self.holds.append_event(
            hold_id,
            "reactivated",
            principal.user_id,
            now,
            {"review_by": review_by, "newly_attached": len(to_attach)},
        )
        updated = self.holds.get(hold_id)
        paused = self._pause_plans_for([item["dossier_id"] for item in self.holds.items(hold_id)], now, principal.user_id, updated)
        if paused:
            self.holds.append_event(hold_id, "plans_auto_paused", principal.user_id, now, {"plan_ids": paused})
        self.audit.record(principal, "legal_hold.reactivate", "legal_hold", str(hold_id), before=hold, after=updated)
        return self._detail(updated)


class RetentionAssessmentService:
    """保存期限到期评估：到期但处于生效保全中的档案不得销毁。"""

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.holds = LegalHoldRepository(connection)
        self.dossiers = DossierRepository(connection)
        self.audit = AuditService(connection, self.clock)

    def assess_dossier(self, principal: Principal, dossier_id: int) -> dict[str, Any]:
        principal.require("retention.assess")
        dossier = self.dossiers.get(dossier_id)
        assessment = self._assess(principal, dossier)
        self.audit.record(
            principal,
            "retention.assess",
            "dossier",
            str(dossier_id),
            after=assessment,
            metadata={"outcome": assessment["outcome"]},
        )
        return assessment

    def assess_due(self, principal: Principal, as_of: str | None) -> dict[str, Any]:
        principal.require("retention.assess")
        moment = require_iso_moment(as_of, "评估基准时间") if as_of else to_storage(self.clock.now())
        rows = self.connection.execute(
            """SELECT * FROM dossiers
               WHERE retention_until IS NOT NULL AND retention_until<=?
               AND lifecycle_state NOT IN ('disposed') ORDER BY retention_until,id""",
            (moment,),
        ).fetchall()
        assessments = [self._assess(principal, dict(row), moment=moment) for row in rows]
        summary = {"not_due": 0, "held": 0, "eligible": 0}
        for item in assessments:
            summary[item["outcome"]] += 1
        self.audit.record(
            principal,
            "retention.assess_due",
            "retention_assessment",
            None,
            metadata={"as_of": moment, **summary},
        )
        return {"as_of": moment, "summary": summary, "assessments": assessments}

    def list(self, principal: Principal, dossier_id: int | None) -> list[dict[str, Any]]:
        principal.require("dossiers.read")
        if dossier_id:
            rows = self.connection.execute(
                "SELECT * FROM retention_assessments WHERE dossier_id=? ORDER BY id DESC LIMIT 200", (dossier_id,)
            ).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM retention_assessments ORDER BY id DESC LIMIT 200").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result

    def _assess(self, principal: Principal, dossier: dict[str, Any], moment: str | None = None) -> dict[str, Any]:
        now = moment or to_storage(self.clock.now())
        retention_until = dossier.get("retention_until")
        active_holds = self.holds.active_for_dossier(dossier["id"])
        if not retention_until or retention_until > now:
            outcome = "not_due"
        elif active_holds:
            outcome = "held"
        else:
            outcome = "eligible"
        details = {
            "dossier_code": dossier["dossier_code"],
            "lifecycle_state": dossier["lifecycle_state"],
            "secrecy_level": dossier["secrecy_level"],
            "active_holds": [
                {"hold_id": hold["id"], "hold_code": hold["hold_code"], "source_reference": hold["source_reference"]}
                for hold in active_holds
            ],
        }
        cursor = self.connection.execute(
            """INSERT INTO retention_assessments(assessment_code,dossier_id,retention_until,assessed_at,outcome,
               active_hold_count,details_json,assessed_by)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                f"RA-{uuid.uuid4().hex[:12].upper()}",
                dossier["id"],
                retention_until,
                now,
                outcome,
                len(active_holds),
                json.dumps(details, ensure_ascii=False),
                principal.user_id,
            ),
        )
        row = row_dict(
            self.connection.execute("SELECT * FROM retention_assessments WHERE id=?", (cursor.lastrowid,)).fetchone()
        )
        row["details"] = json.loads(row.pop("details_json"))
        return row


class DisposalPlanService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.dossiers = DossierRepository(connection)
        self.holds = LegalHoldRepository(connection)
        self.approvals = ApprovalRepository(connection)
        self.audit = AuditService(connection, self.clock)

    # ------------------------------------------------------------------ 查询
    def get(self, principal: Principal, plan_id: int) -> dict[str, Any]:
        principal.require("dossiers.read")
        plan = self._get_plan(plan_id)
        self._sync_pause(plan)
        return self._detail(self._get_plan(plan_id))

    def list(self, principal: Principal, state: str | None, dossier_id: int | None) -> list[dict[str, Any]]:
        principal.require("dossiers.read")
        clauses: list[str] = []
        params: list[Any] = []
        if state:
            clauses.append("state=?")
            params.append(state)
        if dossier_id:
            clauses.append("dossier_id=?")
            params.append(dossier_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(500)
        rows = self.connection.execute(
            "SELECT * FROM disposal_plans" + where + " ORDER BY id DESC LIMIT ?", tuple(params)
        ).fetchall()
        plans = []
        for row in rows:
            plan = dict(row)
            self._sync_pause(plan)
            plans.append(self._present(self._get_plan(plan["id"])))
        return plans

    def _get_plan(self, plan_id: int) -> dict[str, Any]:
        return row_dict(self.connection.execute("SELECT * FROM disposal_plans WHERE id=?", (plan_id,)).fetchone())

    def _present(self, plan: dict[str, Any]) -> dict[str, Any]:
        presented = dict(plan)
        presented["eligibility"] = json.loads(presented.pop("eligibility_json"))
        return presented

    def _detail(self, plan: dict[str, Any]) -> dict[str, Any]:
        detail = self._present(plan)
        rows = self.connection.execute(
            "SELECT * FROM disposal_plan_events WHERE plan_id=? ORDER BY id", (plan["id"],)
        ).fetchall()
        events = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            events.append(item)
        detail["events"] = events
        return detail

    def _append_event(
        self,
        plan_id: int,
        event_type: str,
        actor_user_id: int | None,
        now: str,
        *,
        from_state: str | None = None,
        to_state: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.connection.execute(
            """INSERT INTO disposal_plan_events(plan_id,event_type,from_state,to_state,actor_user_id,details_json,occurred_at)
               VALUES(?,?,?,?,?,?,?)""",
            (plan_id, event_type, from_state, to_state, actor_user_id, json.dumps(details or {}, ensure_ascii=False), now),
        )

    def _sync_pause(self, plan: dict[str, Any]) -> None:
        """保全重新生效或档案版本变化时自动暂停待执行计划。"""
        if plan["state"] != "scheduled":
            return
        dossier = self.dossiers.get(plan["dossier_id"])
        now = to_storage(self.clock.now())
        active_holds = self.holds.active_for_dossier(plan["dossier_id"])
        if active_holds:
            auto_pause_plans(
                self.connection,
                plan["dossier_id"],
                "legal_hold_activated",
                now,
                details={"hold_codes": [hold["hold_code"] for hold in active_holds]},
            )
            return
        if dossier["version"] != plan["dossier_version"]:
            auto_pause_plans(
                self.connection,
                plan["dossier_id"],
                "dossier_version_changed",
                now,
                details={"plan_version": plan["dossier_version"], "current_version": dossier["version"]},
            )

    # ------------------------------------------------------------------ 资格判断
    def _evaluate_eligibility(self, dossier: dict[str, Any], approval: dict[str, Any], planned_quantity: float) -> dict[str, Any]:
        active_holds = self.holds.active_for_dossier(dossier["id"])
        approvers = sorted(
            {
                decision["approver_user_id"]
                for decision in approval["decisions"]
                if decision["decision"] == "approve"
            }
        )
        checks = [
            {
                "check": "no_active_legal_hold",
                "passed": not active_holds,
                "detail": "无生效中的法律保全"
                if not active_holds
                else "仍被保全覆盖：" + ",".join(hold["hold_code"] for hold in active_holds),
            },
            {
                "check": "secrecy_level_disposable",
                "passed": dossier["secrecy_level"] not in NON_DISPOSABLE_SECRECY_LEVELS,
                "detail": f"密级 {dossier['secrecy_level']} 可进入处置"
                if dossier["secrecy_level"] not in NON_DISPOSABLE_SECRECY_LEVELS
                else "绝密载体必须先降密才能处置",
            },
            {
                "check": "dual_review_approved",
                "passed": approval["state"] == "approved" and len(approvers) >= 2,
                "detail": f"双人复核审批单 {approval['request_code']} 状态 {approval['state']}，复核人 {approvers}",
            },
            {
                "check": "lifecycle_state_allows",
                "passed": dossier["lifecycle_state"] in PLAN_ALLOWED_STATES,
                "detail": f"当前状态 {dossier['lifecycle_state']}",
            },
            {
                "check": "quantity_available",
                "passed": 0 < planned_quantity <= dossier["quantity"] - dossier["reserved_quantity"],
                "detail": f"计划数量 {planned_quantity}，可用数量 {dossier['quantity'] - dossier['reserved_quantity']}",
            },
        ]
        return {
            "judged_at": to_storage(self.clock.now()),
            "dossier_id": dossier["id"],
            "dossier_code": dossier["dossier_code"],
            "dossier_version": dossier["version"],
            "secrecy_level": dossier["secrecy_level"],
            "approval_request_id": approval["id"],
            "approver_user_ids": approvers,
            "checks": checks,
            "eligible": all(check["passed"] for check in checks),
        }

    # ------------------------------------------------------------------ 生成
    def create(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("disposal_plans.manage")
        plan_code = require_code(data["plan_code"], "计划编号") if data.get("plan_code") else f"DP-{uuid.uuid4().hex[:12].upper()}"
        existing = self.connection.execute("SELECT * FROM disposal_plans WHERE plan_code=?", (plan_code,)).fetchone()
        if existing:
            existing = dict(existing)
            same = (
                existing["dossier_id"] == data["dossier_id"]
                and existing["approval_request_id"] == data["approval_request_id"]
                and abs(existing["planned_quantity"] - data["planned_quantity"]) < 1e-9
                and existing["method"] == data["method"]
            )
            if not same:
                raise ConflictError("计划编号已被不同的处置计划占用")
            return {"plan": self._detail(existing), "replayed": True}
        dossier = self.dossiers.get(data["dossier_id"])
        approval = self.approvals.get(data["approval_request_id"])
        if approval["action_type"] != "disposal" or approval["resource_type"] != "dossier" or approval["resource_id"] != dossier["id"]:
            raise ValidationError("审批单必须是针对该档案的合规处置双人复核")
        approval_quantity = approval["payload"].get("quantity")
        if approval_quantity is not None and abs(float(approval_quantity) - data["planned_quantity"]) > 1e-9:
            raise ValidationError("计划数量必须与审批单批准的处置数量一致")
        duplicate = self.connection.execute(
            """SELECT id FROM disposal_plans
               WHERE state IN ('scheduled','paused') AND (dossier_id=? OR approval_request_id=?) LIMIT 1""",
            (dossier["id"], approval["id"]),
        ).fetchone()
        if duplicate:
            raise ConflictError("该档案或审批单已存在未结束的处置计划")
        eligibility = self._evaluate_eligibility(dossier, approval, data["planned_quantity"])
        if not eligibility["eligible"]:
            failed = [check["check"] for check in eligibility["checks"] if not check["passed"]]
            raise ConflictError("载体不满足进入处置计划的条件", context={"failed_checks": failed, "eligibility": eligibility})
        now = to_storage(self.clock.now())
        cursor = self.connection.execute(
            """INSERT INTO disposal_plans(plan_code,dossier_id,planned_quantity,method,state,eligibility_json,
               dossier_version,approval_request_id,created_by,created_at,updated_at)
               VALUES(?,?,?,?,'scheduled',?,?,?,?,?,?)""",
            (
                plan_code,
                dossier["id"],
                data["planned_quantity"],
                data["method"],
                json.dumps(eligibility, ensure_ascii=False),
                dossier["version"],
                approval["id"],
                principal.user_id,
                now,
                now,
            ),
        )
        plan_id = cursor.lastrowid
        self._append_event(plan_id, "created", principal.user_id, now, to_state="scheduled", details={"eligibility": eligibility})
        plan = self._get_plan(plan_id)
        self.audit.record(principal, "disposal_plan.create", "disposal_plan", str(plan_id), after=plan)
        return {"plan": self._detail(plan), "replayed": False}

    # ------------------------------------------------------------------ 恢复
    def resume(self, principal: Principal, plan_id: int) -> dict[str, Any]:
        principal.require("disposal_plans.manage")
        plan = self._get_plan(plan_id)
        if plan["state"] != "paused":
            raise ConflictError("只有已暂停的处置计划可以恢复")
        if plan["pause_reason"] != "legal_hold_activated":
            raise ConflictError("档案版本已变化，资格判断失效，请作废后重新生成处置计划")
        dossier = self.dossiers.get(plan["dossier_id"])
        if dossier["version"] != plan["dossier_version"]:
            raise ConflictError("档案版本已变化，资格判断失效，请作废后重新生成处置计划")
        if self.holds.active_for_dossier(plan["dossier_id"]):
            raise ConflictError("档案仍处于法律保全状态，不能恢复处置计划")
        now = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE disposal_plans SET state='scheduled',pause_reason=NULL,version=version+1,updated_at=? WHERE id=?",
            (now, plan_id),
        )
        self._append_event(plan_id, "resumed", principal.user_id, now, from_state="paused", to_state="scheduled")
        updated = self._get_plan(plan_id)
        self.audit.record(principal, "disposal_plan.resume", "disposal_plan", str(plan_id), before=plan, after=updated)
        return self._detail(updated)

    # ------------------------------------------------------------------ 作废
    def cancel(self, principal: Principal, plan_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("disposal_plans.manage")
        plan = self._get_plan(plan_id)
        if plan["state"] in {"executed", "cancelled"}:
            raise ConflictError("历史处置计划不能被覆盖或重复作废")
        now = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE disposal_plans SET state='cancelled',version=version+1,updated_at=? WHERE id=?",
            (now, plan_id),
        )
        self._append_event(
            plan_id,
            "cancelled",
            principal.user_id,
            now,
            from_state=plan["state"],
            to_state="cancelled",
            details={"reason": data["reason"].strip()},
        )
        updated = self._get_plan(plan_id)
        self.audit.record(principal, "disposal_plan.cancel", "disposal_plan", str(plan_id), before=plan, after=updated)
        return self._detail(updated)

    # ------------------------------------------------------------------ 执行
    def execute(self, principal: Principal, plan_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("disposal_plans.manage")
        plan = self._get_plan(plan_id)
        if plan["state"] in {"executed", "cancelled"}:
            raise ConflictError("历史处置计划不能被覆盖或重复执行")
        self._sync_pause(plan)
        plan = self._get_plan(plan_id)
        if plan["state"] != "scheduled":
            raise ConflictError("处置计划已暂停，请先解除保全或恢复计划")
        result = DisposalService(self.connection, self.clock).execute(
            principal,
            plan["approval_request_id"],
            {"method": plan["method"], "witness_one": data["witness_one"], "witness_two": data["witness_two"]},
        )
        now = to_storage(self.clock.now())
        self.connection.execute(
            """UPDATE disposal_plans SET state='executed',executed_at=?,disposal_record_id=?,
               version=version+1,updated_at=? WHERE id=?""",
            (now, result["record"]["id"], now, plan_id),
        )
        self._append_event(
            plan_id,
            "executed",
            principal.user_id,
            now,
            from_state="scheduled",
            to_state="executed",
            details={"disposal_record_id": result["record"]["id"], "certificate_digest": result["record"]["certificate_digest"]},
        )
        updated = self._get_plan(plan_id)
        self.audit.record(principal, "disposal_plan.execute", "disposal_plan", str(plan_id), before=plan, after=updated)
        return {"plan": self._detail(updated), "disposal": result}

    # ------------------------------------------------------------------ 执行核对
    def verification(self, principal: Principal, plan_id: int) -> dict[str, Any]:
        principal.require("dossiers.read")
        plan = self._get_plan(plan_id)
        if plan["state"] != "executed" or plan["disposal_record_id"] is None:
            raise ConflictError("处置计划尚未执行，无法核对")
        record = row_dict(
            self.connection.execute("SELECT * FROM disposal_records WHERE id=?", (plan["disposal_record_id"],)).fetchone()
        )
        dossier = self.dossiers.get(record["dossier_id"])
        expected_digest = DisposalService.certificate_digest(
            record["request_id"],
            record["dossier_id"],
            record["disposed_quantity"],
            record["method"],
            record["witness_one"],
            record["witness_two"],
            record["disposed_at"],
        )
        witnesses = {
            row["id"]: row["display_name"]
            for row in self.connection.execute(
                "SELECT id,display_name FROM users WHERE id IN (?,?)",
                (record["witness_one"], record["witness_two"]),
            ).fetchall()
        }
        return {
            "plan": self._detail(plan),
            "disposal_record": record,
            "actual_dossier": {"id": dossier["id"], "dossier_code": dossier["dossier_code"], "lifecycle_state": dossier["lifecycle_state"]},
            "witnesses": {
                "witness_one": {"user_id": record["witness_one"], "display_name": witnesses.get(record["witness_one"])},
                "witness_two": {"user_id": record["witness_two"], "display_name": witnesses.get(record["witness_two"])},
            },
            "certificate_digest": record["certificate_digest"],
            "digest_valid": expected_digest == record["certificate_digest"],
        }
