"""法律保全与处置计划。

法务收到诉讼或监管调查通知后，可按项目、家族或泄密事件冻结一组档案；
保全期间档案不得因保存期限到期而被销毁。解除保全后，满足密级与双人
复核条件的载体才能生成处置计划。计划在生成时记录资格判断快照，保全
重新生效或档案版本变化时自动暂停，执行后保留可核对的处置证明，历史
计划与决定只增不改。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import Principal
from app.archives.operations import DisposalService, disposal_certificate_digest
from app.archives.repository import ApprovalRepository, DossierRepository, row_dict
from app.services.audit import AuditService

# 处置计划密级策略：绝密载体须先降密，不得进入常规处置计划。
DISPOSAL_PLAN_SECRECY_POLICY = "绝密(top_secret)载体须先降密，不得进入处置计划"
BLOCKED_SECRECY_LEVELS = {"top_secret"}

PLAN_PAUSE_HOLD = "legal_hold_activated"
PLAN_PAUSE_VERSION = "dossier_version_changed"


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _eligibility_digest(evaluation: dict[str, Any]) -> str:
    canonical = json.dumps(evaluation, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class LegalHoldRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create(self, data: dict[str, Any], created_by: int, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO legal_holds(hold_code,matter_reference,notice_type,reason,scope_type,scope_key,
               review_until,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,'active',?,?)""",
            (
                data["hold_code"], data["matter_reference"], data["notice_type"], data["reason"],
                data["scope_type"], data["scope_key"], data["review_until"], created_by, now,
            ),
        )
        return self.get(cursor.lastrowid)

    def get(self, hold_id: int) -> dict[str, Any]:
        return row_dict(
            self.connection.execute("SELECT * FROM legal_holds WHERE id=?", (hold_id,)).fetchone()
        )

    def by_code(self, hold_code: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM legal_holds WHERE hold_code=?", (hold_code,)).fetchone()
        return dict(row) if row else None

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        sql = """SELECT h.*,
                        (SELECT COUNT(*) FROM legal_hold_items i WHERE i.hold_id=h.id) AS item_count,
                        (SELECT COUNT(*) FROM legal_hold_items i WHERE i.hold_id=h.id AND i.released_at IS NULL) AS active_item_count
                 FROM legal_holds h"""
        params: list[Any] = []
        if status:
            sql += " WHERE h.status=?"
            params.append(status)
        sql += " ORDER BY h.id DESC"
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    def has_active_duplicate(self, matter_reference: str, scope_type: str, scope_key: str) -> bool:
        row = self.connection.execute(
            """SELECT 1 FROM legal_holds
               WHERE matter_reference=? AND scope_type=? AND scope_key=? AND status='active' LIMIT 1""",
            (matter_reference, scope_type, scope_key),
        ).fetchone()
        return row is not None

    def attach(self, hold_id: int, dossier_id: int, now: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO legal_hold_items(hold_id,dossier_id,attached_at) VALUES(?,?,?)",
            (hold_id, dossier_id, now),
        )

    def items(self, hold_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT i.id,i.hold_id,i.dossier_id,i.attached_at,i.released_at,
                      d.dossier_code,d.asset_type,d.secrecy_level,d.lifecycle_state,d.quantity,d.unit
               FROM legal_hold_items i JOIN dossiers d ON d.id=i.dossier_id
               WHERE i.hold_id=? ORDER BY i.id""",
            (hold_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def item_dossier_ids(self, hold_id: int) -> list[int]:
        rows = self.connection.execute(
            "SELECT dossier_id FROM legal_hold_items WHERE hold_id=? ORDER BY id", (hold_id,)
        ).fetchall()
        return [int(row[0]) for row in rows]

    def active_holds_for_dossier(self, dossier_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT h.* FROM legal_holds h JOIN legal_hold_items i ON i.hold_id=h.id
               WHERE i.dossier_id=? AND h.status='active' AND i.released_at IS NULL ORDER BY h.id""",
            (dossier_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def holds_for_dossier(self, dossier_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT h.*,i.attached_at AS item_attached_at,i.released_at AS item_released_at
               FROM legal_hold_items i JOIN legal_holds h ON h.id=i.hold_id
               WHERE i.dossier_id=? ORDER BY i.id DESC""",
            (dossier_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def add_extension(
        self,
        hold_id: int,
        previous_review_until: str,
        new_review_until: str,
        reason: str,
        extended_by: int,
        now: str,
    ) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO legal_hold_extensions(hold_id,previous_review_until,new_review_until,reason,extended_by,created_at)
               VALUES(?,?,?,?,?,?)""",
            (hold_id, previous_review_until, new_review_until, reason, extended_by, now),
        )
        return dict(
            self.connection.execute(
                "SELECT * FROM legal_hold_extensions WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
        )

    def update_review_until(self, hold_id: int, new_review_until: str, now: str) -> None:
        self.connection.execute(
            "UPDATE legal_holds SET review_until=?,version=version+1 WHERE id=?",
            (new_review_until, hold_id),
        )

    def extensions(self, hold_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM legal_hold_extensions WHERE hold_id=? ORDER BY id", (hold_id,)
        ).fetchall()
        return [dict(row) for row in rows]


class DisposalPlanRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    @staticmethod
    def _parse(row: dict[str, Any]) -> dict[str, Any]:
        row["eligibility"] = json.loads(row.pop("eligibility_json"))
        return row

    def create(self, data: dict[str, Any], now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO disposal_plans(plan_code,dossier_id,approval_request_id,eligibility_json,
               eligibility_digest,dossier_version,state,created_by,created_at,updated_at)
               VALUES(?,?,?,?,?,?,'ready',?,?,?)""",
            (
                data["plan_code"], data["dossier_id"], data["approval_request_id"],
                json.dumps(data["eligibility"], ensure_ascii=False, sort_keys=True),
                data["eligibility_digest"], data["dossier_version"], data["created_by"], now, now,
            ),
        )
        return self.get(cursor.lastrowid)

    def get(self, plan_id: int) -> dict[str, Any]:
        return self._parse(
            row_dict(self.connection.execute("SELECT * FROM disposal_plans WHERE id=?", (plan_id,)).fetchone())
        )

    def by_code(self, plan_code: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM disposal_plans WHERE plan_code=?", (plan_code,)).fetchone()
        return self._parse(dict(row)) if row else None

    def find_active_for_dossier(self, dossier_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            """SELECT * FROM disposal_plans
               WHERE dossier_id=? AND state IN ('ready','paused','executing') ORDER BY id DESC LIMIT 1""",
            (dossier_id,),
        ).fetchone()
        return self._parse(dict(row)) if row else None

    def ready_for_dossiers(self, dossier_ids: list[int]) -> list[dict[str, Any]]:
        if not dossier_ids:
            return []
        placeholders = ",".join("?" for _ in dossier_ids)
        rows = self.connection.execute(
            f"SELECT * FROM disposal_plans WHERE state='ready' AND dossier_id IN ({placeholders}) ORDER BY id",
            tuple(dossier_ids),
        ).fetchall()
        return [self._parse(dict(row)) for row in rows]

    def list(self, state: str | None = None, dossier_id: int | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if state:
            clauses.append("p.state=?")
            params.append(state)
        if dossier_id:
            clauses.append("p.dossier_id=?")
            params.append(dossier_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            """SELECT p.*,d.dossier_code FROM disposal_plans p JOIN dossiers d ON d.id=p.dossier_id"""
            + where
            + " ORDER BY p.id DESC",
            tuple(params),
        ).fetchall()
        return [self._parse(dict(row)) for row in rows]

    def append_event(
        self,
        plan_id: int,
        event_type: str,
        actor_user_id: int | None,
        now: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.connection.execute(
            """INSERT INTO disposal_plan_events(plan_id,event_type,actor_user_id,details_json,occurred_at)
               VALUES(?,?,?,?,?)""",
            (plan_id, event_type, actor_user_id, json.dumps(details or {}, ensure_ascii=False), now),
        )

    def events(self, plan_id: int) -> list[dict[str, Any]]:
        result = []
        for row in self.connection.execute(
            "SELECT * FROM disposal_plan_events WHERE plan_id=? ORDER BY id", (plan_id,)
        ).fetchall():
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result


class LegalHoldService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.holds = LegalHoldRepository(connection)
        self.plans = DisposalPlanRepository(connection)
        self.dossiers = DossierRepository(connection)
        self.audit = AuditService(connection, self.clock)

    def create(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("legal_holds.manage")
        scope = data["scope"]
        scope_key = str(scope["key"]).strip()
        hold_code = data.get("hold_code") or f"LH-{uuid.uuid4().hex[:12]}"
        existing = self.holds.by_code(hold_code)
        if existing:
            same = (
                existing["matter_reference"] == data["matter_reference"]
                and existing["notice_type"] == data["notice_type"]
                and existing["scope_type"] == scope["type"]
                and existing["scope_key"] == scope_key
            )
            if not same:
                raise ConflictError("保全编号已被其他保全占用")
            return {**self._detail(existing["id"]), "replayed": True}
        if self.holds.has_active_duplicate(data["matter_reference"], scope["type"], scope_key):
            raise ConflictError("相同案件与范围的保全已经存在，请勿重复冻结")
        dossier_ids = self._resolve_scope(scope["type"], scope_key)
        now = to_storage(self.clock.now())
        hold = self.holds.create(
            {
                "hold_code": hold_code,
                "matter_reference": data["matter_reference"],
                "notice_type": data["notice_type"],
                "reason": data["reason"],
                "scope_type": scope["type"],
                "scope_key": scope_key,
                "review_until": data["review_until"],
            },
            principal.user_id,
            now,
        )
        for dossier_id in dossier_ids:
            self.holds.attach(hold["id"], dossier_id, now)
            self.dossiers.append_event(
                dossier_id,
                "legal_hold.attached",
                principal.user_id,
                now,
                details={
                    "hold_id": hold["id"],
                    "hold_code": hold["hold_code"],
                    "matter_reference": data["matter_reference"],
                    "scope_type": scope["type"],
                    "scope_key": scope_key,
                },
            )
        paused_plan_ids = self._pause_ready_plans(
            dossier_ids, principal.user_id, now, {"hold_code": hold["hold_code"]}
        )
        self.audit.record(
            principal,
            "legal_hold.create",
            "legal_hold",
            str(hold["id"]),
            after=hold,
            metadata={"dossier_ids": dossier_ids, "paused_plan_ids": paused_plan_ids},
        )
        return {**self._detail(hold["id"]), "replayed": False}

    def list(self, principal: Principal, status: str | None) -> list[dict[str, Any]]:
        principal.require("dossiers.read")
        return self.holds.list(status)

    def detail(self, principal: Principal, hold_id: int) -> dict[str, Any]:
        principal.require("dossiers.read")
        return self._detail(hold_id)

    def _detail(self, hold_id: int) -> dict[str, Any]:
        hold = self.holds.get(hold_id)
        hold["items"] = self.holds.items(hold_id)
        hold["extensions"] = self.holds.extensions(hold_id)
        return hold

    def for_dossier(self, principal: Principal, dossier_id: int) -> list[dict[str, Any]]:
        principal.require("dossiers.read")
        self.dossiers.get(dossier_id)
        return self.holds.holds_for_dossier(dossier_id)

    def extend(self, principal: Principal, hold_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("legal_holds.manage")
        hold = self.holds.get(hold_id)
        if hold["status"] != "active":
            raise ConflictError("保全已经解除，无法延期")
        if _parse_iso(data["new_review_until"]) <= _parse_iso(hold["review_until"]):
            raise ValidationError("新的复核期限必须晚于当前复核期限")
        now = to_storage(self.clock.now())
        extension = self.holds.add_extension(
            hold_id, hold["review_until"], data["new_review_until"], data["reason"], principal.user_id, now
        )
        self.holds.update_review_until(hold_id, data["new_review_until"], now)
        updated = self.holds.get(hold_id)
        self.audit.record(
            principal,
            "legal_hold.extend",
            "legal_hold",
            str(hold_id),
            before=hold,
            after=updated,
            metadata={"new_review_until": data["new_review_until"]},
        )
        return {"hold": self._detail(hold_id), "extension": extension}

    def release(self, principal: Principal, hold_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("legal_holds.manage")
        hold = self.holds.get(hold_id)
        now = to_storage(self.clock.now())
        dossier_ids = self.holds.item_dossier_ids(hold_id)
        cursor = self.connection.execute(
            """UPDATE legal_holds SET status='released',released_by=?,released_at=?,release_reason=?,version=version+1
               WHERE id=? AND status='active'""",
            (principal.user_id, now, data["reason"], hold_id),
        )
        if cursor.rowcount != 1:
            raise ConflictError("保全已经解除")
        self.connection.execute(
            "UPDATE legal_hold_items SET released_at=? WHERE hold_id=? AND released_at IS NULL",
            (now, hold_id),
        )
        for dossier_id in dossier_ids:
            self.dossiers.append_event(
                dossier_id,
                "legal_hold.released",
                principal.user_id,
                now,
                details={"hold_id": hold_id, "hold_code": hold["hold_code"], "reason": data["reason"]},
            )
        updated = self.holds.get(hold_id)
        self.audit.record(principal, "legal_hold.release", "legal_hold", str(hold_id), before=hold, after=updated)
        return self._detail(hold_id)

    def _resolve_scope(self, scope_type: str, scope_key: str) -> list[int]:
        if scope_type == "project":
            rows = self.connection.execute(
                """SELECT d.id FROM dossiers d JOIN intake_batches b ON b.id=d.intake_id
                   WHERE b.project_code=? AND d.lifecycle_state!='disposed' ORDER BY d.id""",
                (scope_key,),
            ).fetchall()
            dossier_ids = [int(row["id"]) for row in rows]
        elif scope_type == "family":
            try:
                anchor_id = int(scope_key)
            except ValueError as exc:
                raise ValidationError("家族范围必须填写档案 ID") from exc
            anchor = self.dossiers.get(anchor_id)
            root_id = anchor["root_dossier_id"] or anchor["id"]
            rows = self.connection.execute(
                """SELECT id FROM dossiers
                   WHERE (root_dossier_id=? OR id=?) AND lifecycle_state!='disposed' ORDER BY id""",
                (root_id, root_id),
            ).fetchall()
            dossier_ids = [int(row["id"]) for row in rows]
        else:
            try:
                case_id = int(scope_key)
            except ValueError as exc:
                raise ValidationError("事件范围必须填写泄密事件 ID") from exc
            case = self.connection.execute("SELECT * FROM incident_cases WHERE id=?", (case_id,)).fetchone()
            if not case:
                raise NotFoundError("泄密事件不存在")
            dossier_ids_set: set[int] = set()
            if case["dossier_id"]:
                dossier = self.dossiers.get(int(case["dossier_id"]))
                if dossier["lifecycle_state"] != "disposed":
                    dossier_ids_set.add(int(dossier["id"]))
            if case["intake_id"]:
                rows = self.connection.execute(
                    "SELECT id FROM dossiers WHERE intake_id=? AND lifecycle_state!='disposed' ORDER BY id",
                    (case["intake_id"],),
                ).fetchall()
                dossier_ids_set.update(int(row["id"]) for row in rows)
            dossier_ids = sorted(dossier_ids_set)
        if not dossier_ids:
            raise ValidationError("保全范围未匹配到任何在册档案")
        return dossier_ids

    def _pause_ready_plans(
        self,
        dossier_ids: list[int],
        actor_user_id: int,
        now: str,
        extra: dict[str, Any],
    ) -> list[int]:
        paused: list[int] = []
        for plan in self.plans.ready_for_dossiers(dossier_ids):
            cursor = self.connection.execute(
                """UPDATE disposal_plans SET state='paused',pause_reason=?,version=version+1,updated_at=?
                   WHERE id=? AND state='ready'""",
                (PLAN_PAUSE_HOLD, now, plan["id"]),
            )
            if cursor.rowcount == 1:
                self.plans.append_event(
                    plan["id"],
                    "paused",
                    actor_user_id,
                    now,
                    details={"reasons": [PLAN_PAUSE_HOLD], **extra},
                )
                paused.append(int(plan["id"]))
        return paused


class RetentionEvaluationService:
    """保存期限到期评估：到期载体只有在没有有效保全时才允许进入处置环节。"""

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.holds = LegalHoldRepository(connection)

    def evaluate(self, principal: Principal, as_of: str | None) -> dict[str, Any]:
        principal.require("dossiers.read")
        as_of = as_of or to_storage(self.clock.now())
        try:
            datetime.fromisoformat(as_of)
        except ValueError as exc:
            raise ValidationError("评估基准时间必须是 ISO-8601 日期或时间") from exc
        rows = self.connection.execute(
            """SELECT id,dossier_code,asset_type,secrecy_level,lifecycle_state,retention_until,quantity,unit
               FROM dossiers
               WHERE retention_until IS NOT NULL AND retention_until<=? AND lifecycle_state!='disposed'
               ORDER BY retention_until,id""",
            (as_of,),
        ).fetchall()
        items = []
        for row in rows:
            holds = self.holds.active_holds_for_dossier(int(row["id"]))
            items.append(
                {
                    "dossier_id": row["id"],
                    "dossier_code": row["dossier_code"],
                    "asset_type": row["asset_type"],
                    "secrecy_level": row["secrecy_level"],
                    "lifecycle_state": row["lifecycle_state"],
                    "retention_until": row["retention_until"],
                    "quantity": row["quantity"],
                    "unit": row["unit"],
                    "active_hold_codes": [hold["hold_code"] for hold in holds],
                    "evaluation": "blocked_by_hold" if holds else "eligible_for_disposal_plan",
                }
            )
        blocked = sum(1 for item in items if item["evaluation"] == "blocked_by_hold")
        return {
            "as_of": as_of,
            "expired_count": len(items),
            "blocked_by_hold_count": blocked,
            "eligible_count": len(items) - blocked,
            "items": items,
        }


class DisposalPlanService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.plans = DisposalPlanRepository(connection)
        self.holds = LegalHoldRepository(connection)
        self.dossiers = DossierRepository(connection)
        self.approvals = ApprovalRepository(connection)
        self.audit = AuditService(connection, self.clock)

    def create(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("dossiers.dispose")
        dossier = self.dossiers.get(data["dossier_id"])
        if dossier["lifecycle_state"] == "disposed":
            raise ConflictError("档案已完成处置，无法生成处置计划")
        approval = self.approvals.get(data["approval_request_id"])
        if (
            approval["action_type"] != "disposal"
            or approval["resource_type"] != "dossier"
            or int(approval["resource_id"]) != int(dossier["id"])
        ):
            raise ValidationError("审批请求与档案处置计划不匹配")
        existing = self.plans.find_active_for_dossier(dossier["id"])
        if existing:
            raise ConflictError(
                "该档案已存在进行中的处置计划",
                context={"plan_code": existing["plan_code"], "state": existing["state"]},
            )
        now = to_storage(self.clock.now())
        evaluation = self._evaluate(dossier, approval, now)
        if not evaluation["eligible"]:
            raise ConflictError("档案不满足进入处置计划的条件", context={"eligibility": evaluation})
        plan_code = data.get("plan_code") or f"PLN-{uuid.uuid4().hex[:12]}"
        if self.plans.by_code(plan_code):
            raise ConflictError("处置计划编码已存在")
        plan = self.plans.create(
            {
                "plan_code": plan_code,
                "dossier_id": dossier["id"],
                "approval_request_id": approval["id"],
                "eligibility": evaluation,
                "eligibility_digest": _eligibility_digest(evaluation),
                "dossier_version": dossier["version"],
                "created_by": principal.user_id,
            },
            now,
        )
        self.plans.append_event(plan["id"], "created", principal.user_id, now, details={"eligibility": evaluation})
        self.audit.record(
            principal,
            "disposal_plan.create",
            "disposal_plan",
            str(plan["id"]),
            after=plan,
            metadata={"eligibility_digest": plan["eligibility_digest"]},
        )
        created = self.plans.get(plan["id"])
        created["events"] = self.plans.events(plan["id"])
        return created

    def list(
        self,
        principal: Principal,
        state: str | None,
        dossier_id: int | None,
    ) -> list[dict[str, Any]]:
        principal.require("dossiers.read")
        plans = self.plans.list(state=state, dossier_id=dossier_id)
        return [self._sync(plan, principal.user_id) for plan in plans]

    def detail(self, principal: Principal, plan_id: int) -> dict[str, Any]:
        principal.require("dossiers.read")
        plan = self._sync(self.plans.get(plan_id), principal.user_id)
        plan["events"] = self.plans.events(plan_id)
        return plan

    def execute(self, principal: Principal, plan_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("dossiers.dispose")
        plan = self._sync(self.plans.get(plan_id), principal.user_id)
        if plan["state"] != "ready":
            raise ConflictError(
                "处置计划不在可执行状态",
                context={"state": plan["state"], "pause_reason": plan["pause_reason"]},
            )
        now = to_storage(self.clock.now())
        cursor = self.connection.execute(
            "UPDATE disposal_plans SET state='executing',version=version+1,updated_at=? WHERE id=? AND state='ready'",
            (now, plan["id"]),
        )
        if cursor.rowcount != 1:
            raise ConflictError("处置计划状态已变化，请刷新后重试")
        result = DisposalService(self.connection, self.clock).execute(principal, plan["approval_request_id"], data)
        record = result["record"]
        self.connection.execute(
            """UPDATE disposal_plans SET state='executed',disposal_record_id=?,version=version+1,updated_at=?
               WHERE id=? AND state='executing'""",
            (record["id"], now, plan["id"]),
        )
        self.plans.append_event(
            plan["id"],
            "executed",
            principal.user_id,
            now,
            details={
                "disposal_record_id": record["id"],
                "certificate_digest": record["certificate_digest"],
                "method": record["method"],
                "witness_one": record["witness_one"],
                "witness_two": record["witness_two"],
                "disposed_quantity": record["disposed_quantity"],
            },
        )
        updated = self.plans.get(plan["id"])
        self.audit.record(
            principal,
            "disposal_plan.execute",
            "disposal_plan",
            str(plan["id"]),
            before=plan,
            after=updated,
            metadata={"disposal_record_id": record["id"], "certificate_digest": record["certificate_digest"]},
        )
        updated["events"] = self.plans.events(plan["id"])
        return {"plan": updated, "disposal": result}

    def cancel(self, principal: Principal, plan_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("dossiers.dispose")
        plan = self._sync(self.plans.get(plan_id), principal.user_id)
        if plan["state"] not in {"ready", "paused"}:
            raise ConflictError("只有待执行或已暂停的处置计划可以作废")
        now = to_storage(self.clock.now())
        cursor = self.connection.execute(
            "UPDATE disposal_plans SET state='cancelled',version=version+1,updated_at=? WHERE id=? AND state IN ('ready','paused')",
            (now, plan_id),
        )
        if cursor.rowcount != 1:
            raise ConflictError("处置计划状态已变化，请刷新后重试")
        self.plans.append_event(plan_id, "cancelled", principal.user_id, now, details={"reason": data["reason"]})
        updated = self.plans.get(plan_id)
        self.audit.record(principal, "disposal_plan.cancel", "disposal_plan", str(plan_id), before=plan, after=updated)
        updated["events"] = self.plans.events(plan_id)
        return updated

    def verification(self, principal: Principal, plan_id: int) -> dict[str, Any]:
        principal.require("dossiers.read")
        plan = self._sync(self.plans.get(plan_id), principal.user_id)
        if plan["state"] != "executed" or not plan["disposal_record_id"]:
            raise ConflictError("处置计划尚未执行，无法核对处置证明")
        record = row_dict(
            self.connection.execute(
                "SELECT * FROM disposal_records WHERE id=?", (plan["disposal_record_id"],)
            ).fetchone()
        )
        dossier = self.dossiers.get(plan["dossier_id"])
        recomputed = disposal_certificate_digest(
            request_id=record["request_id"],
            dossier_id=record["dossier_id"],
            quantity=float(record["disposed_quantity"]),
            method=record["method"],
            witness_one=int(record["witness_one"]),
            witness_two=int(record["witness_two"]),
            disposed_at=record["disposed_at"],
        )
        witnesses = []
        for witness_id in (record["witness_one"], record["witness_two"]):
            user = self.connection.execute(
                "SELECT id,username,display_name FROM users WHERE id=?", (witness_id,)
            ).fetchone()
            witnesses.append(dict(user) if user else {"id": witness_id, "username": None, "display_name": None})
        return {
            "plan_id": plan["id"],
            "plan_code": plan["plan_code"],
            "state": plan["state"],
            "eligibility_digest": plan["eligibility_digest"],
            "dossier": {
                "id": dossier["id"],
                "dossier_code": dossier["dossier_code"],
                "lifecycle_state": dossier["lifecycle_state"],
                "quantity": dossier["quantity"],
                "unit": dossier["unit"],
            },
            "disposal_record": record,
            "witnesses": witnesses,
            "certificate_digest": record["certificate_digest"],
            "recomputed_digest": recomputed,
            "digest_match": recomputed == record["certificate_digest"],
        }

    def _evaluate(self, dossier: dict[str, Any], approval: dict[str, Any], now: str) -> dict[str, Any]:
        active_holds = self.holds.active_holds_for_dossier(dossier["id"])
        approvers = sorted(
            {decision["approver_user_id"] for decision in approval["decisions"] if decision["decision"] == "approve"}
        )
        secrecy_satisfied = dossier["secrecy_level"] not in BLOCKED_SECRECY_LEVELS
        hold_clear = not active_holds
        dual_review_satisfied = approval["state"] == "approved" and len(approvers) >= 2
        retention_until = dossier["retention_until"]
        evaluation = {
            "evaluated_at": now,
            "dossier_id": dossier["id"],
            "dossier_code": dossier["dossier_code"],
            "dossier_version": dossier["version"],
            "lifecycle_state": dossier["lifecycle_state"],
            "secrecy_level": dossier["secrecy_level"],
            "secrecy_policy": DISPOSAL_PLAN_SECRECY_POLICY,
            "secrecy_satisfied": secrecy_satisfied,
            "active_hold_codes": [hold["hold_code"] for hold in active_holds],
            "hold_clear": hold_clear,
            "approval_request_id": approval["id"],
            "approval_state": approval["state"],
            "approver_user_ids": approvers,
            "dual_review_satisfied": dual_review_satisfied,
            "retention_until": retention_until,
            "retention_expired": bool(retention_until and retention_until <= now),
        }
        evaluation["eligible"] = bool(secrecy_satisfied and hold_clear and dual_review_satisfied)
        return evaluation

    def _sync(self, plan: dict[str, Any], actor_user_id: int) -> dict[str, Any]:
        """保全重新生效或档案版本变化时，自动暂停待执行计划。"""
        if plan["state"] != "ready":
            return plan
        dossier = self.dossiers.get(plan["dossier_id"])
        reasons: list[str] = []
        active_holds = self.holds.active_holds_for_dossier(plan["dossier_id"])
        if active_holds:
            reasons.append(PLAN_PAUSE_HOLD)
        if int(dossier["version"]) != int(plan["dossier_version"]):
            reasons.append(PLAN_PAUSE_VERSION)
        if not reasons:
            return plan
        now = to_storage(self.clock.now())
        cursor = self.connection.execute(
            """UPDATE disposal_plans SET state='paused',pause_reason=?,version=version+1,updated_at=?
               WHERE id=? AND state='ready'""",
            (",".join(reasons), now, plan["id"]),
        )
        if cursor.rowcount == 1:
            self.plans.append_event(
                plan["id"],
                "paused",
                actor_user_id,
                now,
                details={
                    "reasons": reasons,
                    "dossier_version": dossier["version"],
                    "plan_dossier_version": plan["dossier_version"],
                    "active_hold_codes": [hold["hold_code"] for hold in active_holds],
                },
            )
        return self.plans.get(plan["id"])
