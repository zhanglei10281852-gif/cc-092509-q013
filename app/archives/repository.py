from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.errors import ConflictError, NotFoundError, ValidationError


def row_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        raise NotFoundError("记录不存在")
    return dict(row)


def auto_pause_plans(
    connection: sqlite3.Connection,
    dossier_id: int,
    reason: str,
    now: str,
    *,
    actor_user_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> list[int]:
    """档案版本变化等事件发生时，自动暂停该档案全部待执行处置计划。

    历史决定只追加事件、不覆盖；已暂停或已执行的计划不会被重复暂停。
    """
    plan_ids = [
        row[0]
        for row in connection.execute(
            "SELECT id FROM disposal_plans WHERE dossier_id=? AND state='scheduled' ORDER BY id",
            (dossier_id,),
        ).fetchall()
    ]
    for plan_id in plan_ids:
        connection.execute(
            "UPDATE disposal_plans SET state='paused',pause_reason=?,version=version+1,updated_at=? WHERE id=?",
            (reason, now, plan_id),
        )
        connection.execute(
            """INSERT INTO disposal_plan_events(plan_id,event_type,from_state,to_state,actor_user_id,details_json,occurred_at)
               VALUES(?,?, 'scheduled','paused',?,?,?)""",
            (plan_id, "paused", actor_user_id, json.dumps({"reason": reason, **(details or {})}, ensure_ascii=False), now),
        )
    return plan_ids


class VaultRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create(self, data: dict[str, Any], now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO vault_locations(code,building,room,cabinet,shelf,sensitivity,capacity_units,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                data["code"], data["building"], data["room"], data["cabinet"], data["shelf"],
                data["sensitivity"], data["capacity_units"], now, now,
            ),
        )
        return self.get(cursor.lastrowid)

    def get(self, vault_id: int) -> dict[str, Any]:
        return row_dict(self.connection.execute("SELECT * FROM vault_locations WHERE id=?", (vault_id,)).fetchone())

    def by_code(self, code: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM vault_locations WHERE code=?", (code,)).fetchone()
        return dict(row) if row else None

    def list(self, active_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM vault_locations"
        if active_only:
            sql += " WHERE active=1"
        sql += " ORDER BY code"
        return [dict(row) for row in self.connection.execute(sql).fetchall()]


class IntakeRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create(self, data: dict[str, Any], actor_user_id: int, qr_payload: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO intake_batches(intake_code,project_code,received_by,received_at,expected_count,status,qr_payload,created_at,updated_at)
               VALUES(?,?,?,?,?,'open',?,?,?)""",
            (data["intake_code"], data["project_code"], actor_user_id, now, data["expected_count"], qr_payload, now, now),
        )
        return self.get(cursor.lastrowid)

    def get(self, intake_id: int) -> dict[str, Any]:
        return row_dict(self.connection.execute("SELECT * FROM intake_batches WHERE id=?", (intake_id,)).fetchone())

    def update_counts(self, intake_id: int, now: str) -> dict[str, Any]:
        accepted = self.connection.execute("SELECT COUNT(*) FROM dossiers WHERE intake_id=?", (intake_id,)).fetchone()[0]
        self.connection.execute(
            "UPDATE intake_batches SET accepted_count=?,updated_at=? WHERE id=?",
            (accepted, now, intake_id),
        )
        return self.get(intake_id)


class DossierRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def get(self, dossier_id: int) -> dict[str, Any]:
        return row_dict(
            self.connection.execute(
                """SELECT s.*,b.intake_code,l.code AS vault_code,l.sensitivity AS vault_sensitivity
                   FROM dossiers s JOIN intake_batches b ON b.id=s.intake_id
                   LEFT JOIN vault_locations l ON l.id=s.vault_id WHERE s.id=?""",
                (dossier_id,),
            ).fetchone()
        )

    def by_code(self, dossier_code: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM dossiers WHERE dossier_code=?", (dossier_code,)).fetchone()
        return dict(row) if row else None

    def list(self, *, state: str | None = None, intake_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if state:
            clauses.append("s.lifecycle_state=?")
            params.append(state)
        if intake_id:
            clauses.append("s.intake_id=?")
            params.append(intake_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        rows = self.connection.execute(
            """SELECT s.*,b.intake_code,l.code AS vault_code,l.sensitivity AS vault_sensitivity
               FROM dossiers s JOIN intake_batches b ON b.id=s.intake_id
               LEFT JOIN vault_locations l ON l.id=s.vault_id""" + where + " ORDER BY s.id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def create(self, data: dict[str, Any], now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO dossiers(dossier_code,intake_id,disclosure_event_id,source_dossier_id,root_dossier_id,asset_type,
               quantity,unit,lifecycle_state,vault_id,custody_user_id,provenance_depth,secrecy_level,retention_until,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data["dossier_code"], data["intake_id"], data.get("disclosure_event_id"), data.get("source_dossier_id"),
                data.get("root_dossier_id"), data["asset_type"], data["quantity"], data["unit"], data["lifecycle_state"],
                data.get("vault_id"), data.get("custody_user_id"), data.get("provenance_depth", 0),
                data.get("secrecy_level", "internal"), data.get("retention_until"), now, now,
            ),
        )
        dossier_id = cursor.lastrowid
        if not data.get("root_dossier_id"):
            self.connection.execute("UPDATE dossiers SET root_dossier_id=? WHERE id=?", (dossier_id, dossier_id))
        return self.get(dossier_id)

    def change_quantity(self, dossier_id: int, delta: float, expected_version: int, now: str) -> dict[str, Any]:
        updated = self.connection.execute(
            """UPDATE dossiers SET quantity=quantity+?,version=version+1,updated_at=?
               WHERE id=? AND version=? AND quantity+?>=0 AND reserved_quantity<=quantity+?""",
            (delta, now, dossier_id, expected_version, delta, delta),
        )
        if updated.rowcount != 1:
            raise ConflictError("档案数量或版本已变化，请刷新后重试")
        return self.get(dossier_id)

    def set_state(self, dossier_id: int, state: str, expected_version: int, now: str) -> dict[str, Any]:
        updated = self.connection.execute(
            "UPDATE dossiers SET lifecycle_state=?,version=version+1,updated_at=? WHERE id=? AND version=?",
            (state, now, dossier_id, expected_version),
        )
        if updated.rowcount != 1:
            raise ConflictError("档案状态已变化，请刷新后重试")
        return self.get(dossier_id)

    def children(self, dossier_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM dossiers WHERE source_dossier_id=? ORDER BY id", (dossier_id,)).fetchall()]

    def events(self, dossier_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM dossier_events WHERE dossier_id=? ORDER BY id", (dossier_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result

    def append_event(
        self,
        dossier_id: int,
        event_type: str,
        actor_user_id: int | None,
        now: str,
        *,
        quantity_delta: float = 0,
        from_state: str | None = None,
        to_state: str | None = None,
        details: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> None:
        self.connection.execute(
            """INSERT INTO dossier_events(dossier_id,event_type,actor_user_id,quantity_delta,from_state,to_state,details_json,correlation_id,occurred_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (dossier_id, event_type, actor_user_id, quantity_delta, from_state, to_state, json.dumps(details or {}, ensure_ascii=False), correlation_id, now),
        )


class ApprovalRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create(self, data: dict[str, Any], requested_by: int, request_code: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO approval_requests(request_code,action_type,resource_type,resource_id,requested_by,payload_json,state,
               required_approvals,expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,'pending',2,?,?,?)""",
            (request_code, data["action_type"], data["resource_type"], data["resource_id"], requested_by, json.dumps(data["payload"], ensure_ascii=False), data["expires_at"], now, now),
        )
        return self.get(cursor.lastrowid)

    def get(self, request_id: int) -> dict[str, Any]:
        row = row_dict(self.connection.execute("SELECT * FROM approval_requests WHERE id=?", (request_id,)).fetchone())
        row["payload"] = json.loads(row.pop("payload_json"))
        row["decisions"] = [dict(item) for item in self.connection.execute("SELECT * FROM approval_decisions WHERE request_id=? ORDER BY id", (request_id,)).fetchall()]
        return row

    def decide(self, request_id: int, approver_user_id: int, decision: str, comment: str, now: str) -> dict[str, Any]:
        request = self.get(request_id)
        if request["state"] != "pending":
            raise ConflictError("审批请求已经结束")
        if request["requested_by"] == approver_user_id:
            raise ValidationError("申请人不能审批自己的请求")
        self.connection.execute(
            "INSERT INTO approval_decisions(request_id,approver_user_id,decision,comment,decided_at) VALUES(?,?,?,?,?)",
            (request_id, approver_user_id, decision, comment, now),
        )
        decisions = self.connection.execute("SELECT decision FROM approval_decisions WHERE request_id=?", (request_id,)).fetchall()
        state = "rejected" if any(row[0] == "reject" for row in decisions) else ("approved" if len(decisions) >= request["required_approvals"] else "pending")
        self.connection.execute(
            "UPDATE approval_requests SET state=?,version=version+1,updated_at=? WHERE id=?",
            (state, now, request_id),
        )
        return self.get(request_id)


class LegalHoldRepository:
    """法律保全的持久化与覆盖范围查询，供保全服务与处置服务共用。"""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def by_code(self, hold_code: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM legal_holds WHERE hold_code=?", (hold_code,)).fetchone()
        return dict(row) if row else None

    def get(self, hold_id: int) -> dict[str, Any]:
        return row_dict(self.connection.execute("SELECT * FROM legal_holds WHERE id=?", (hold_id,)).fetchone())

    def create(self, data: dict[str, Any], created_by: int, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO legal_holds(hold_code,scope_type,scope_key,source_type,source_reference,reason,
               state,review_by,created_by,created_at,updated_at)
               VALUES(?,?,?,?,?,?,'active',?,?,?,?)""",
            (
                data["hold_code"], data["scope_type"], data["scope_key"], data["source_type"],
                data["source_reference"], data["reason"], data["review_by"], created_by, now, now,
            ),
        )
        return self.get(cursor.lastrowid)

    def list(self, state: str | None = None, scope_type: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if state:
            clauses.append("state=?")
            params.append(state)
        if scope_type:
            clauses.append("scope_type=?")
            params.append(scope_type)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(500)
        rows = self.connection.execute(
            "SELECT * FROM legal_holds" + where + " ORDER BY id DESC LIMIT ?", tuple(params)
        ).fetchall()
        return [dict(row) for row in rows]

    def attach_items(self, hold_id: int, dossier_ids: list[int], now: str) -> None:
        for dossier_id in dossier_ids:
            self.connection.execute(
                "INSERT OR IGNORE INTO legal_hold_items(hold_id,dossier_id,attached_at) VALUES(?,?,?)",
                (hold_id, dossier_id, now),
            )

    def items(self, hold_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT i.*,s.dossier_code,s.lifecycle_state,s.secrecy_level
               FROM legal_hold_items i JOIN dossiers s ON s.id=i.dossier_id
               WHERE i.hold_id=? ORDER BY s.dossier_code""",
            (hold_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def extensions(self, hold_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM legal_hold_extensions WHERE hold_id=? ORDER BY id", (hold_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def events(self, hold_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM legal_hold_events WHERE hold_id=? ORDER BY id", (hold_id,)
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result

    def append_event(self, hold_id: int, event_type: str, actor_user_id: int | None, now: str, details: dict[str, Any] | None = None) -> None:
        self.connection.execute(
            "INSERT INTO legal_hold_events(hold_id,event_type,actor_user_id,details_json,occurred_at) VALUES(?,?,?,?,?)",
            (hold_id, event_type, actor_user_id, json.dumps(details or {}, ensure_ascii=False), now),
        )

    def active_for_dossier(self, dossier_id: int) -> list[dict[str, Any]]:
        """返回当前覆盖指定档案的全部生效保全（含冻结来源与范围）。"""
        rows = self.connection.execute(
            """SELECT h.* FROM legal_holds h
               JOIN legal_hold_items i ON i.hold_id=h.id
               WHERE i.dossier_id=? AND h.state='active' ORDER BY h.id""",
            (dossier_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def active_hold_ids_for(self, dossier_ids: list[int]) -> dict[int, list[int]]:
        """批量返回 dossier_id -> 生效保全 id 列表，用于范围解析时避免重复冻结。"""
        if not dossier_ids:
            return {}
        placeholders = ",".join("?" for _ in dossier_ids)
        rows = self.connection.execute(
            f"""SELECT i.dossier_id,h.id FROM legal_hold_items i
                JOIN legal_holds h ON h.id=i.hold_id
                WHERE h.state='active' AND i.dossier_id IN ({placeholders})""",
            tuple(dossier_ids),
        ).fetchall()
        result: dict[int, list[int]] = {}
        for row in rows:
            result.setdefault(row[0], []).append(row[1])
        return result


class IncidentRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create(self, data: dict[str, Any], detected_by: int, case_code: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO incident_cases(case_code,dossier_id,intake_id,incident_type,severity,state,detected_by,description,created_at,updated_at)
               VALUES(?,?,?,?,?,'open',?,?,?,?)""",
            (case_code, data.get("dossier_id"), data.get("intake_id"), data["incident_type"], data["severity"], detected_by, data["description"], now, now),
        )
        return self.get(cursor.lastrowid)

    def get(self, case_id: int) -> dict[str, Any]:
        return row_dict(self.connection.execute("SELECT * FROM incident_cases WHERE id=?", (case_id,)).fetchone())

    def list(self, state: str | None = None) -> list[dict[str, Any]]:
        if state:
            rows = self.connection.execute("SELECT * FROM incident_cases WHERE state=? ORDER BY id DESC", (state,)).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM incident_cases ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]
