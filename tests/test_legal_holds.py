from __future__ import annotations

import pytest


PASSWORD = "Passw0rd!2345"


def create_user(client, admin, username, role_codes):
    user = client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": username, "password": PASSWORD, "display_name": username, "role_codes": role_codes},
    )
    assert user.status_code == 201, user.text
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": PASSWORD, "client_label": "tests"},
    )
    assert login.status_code == 200, login.text
    return {"id": user.json()["id"], "headers": {"Authorization": f"Bearer {login.json()['token']}"}}


@pytest.fixture()
def approvers(client, admin):
    return [
        create_user(client, admin, "approver.one", ["approver"]),
        create_user(client, admin, "approver.two", ["approver"]),
    ]


def create_vault(client, admin, code):
    response = client.post(
        "/api/dossiers/vaults",
        headers=admin["headers"],
        json={
            "code": code,
            "building": "档案楼",
            "room": "常温库",
            "cabinet": "一号柜",
            "shelf": "一层",
            "sensitivity": "normal",
            "capacity_units": 100,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_dossier(client, admin, code, project="P-LEGAL", secrecy="internal", retention=None, vault=None):
    vault = vault or create_vault(client, admin, f"V-{code}")
    batch = client.post(
        "/api/dossiers/batches",
        headers=admin["headers"],
        json={"intake_code": f"BATCH-{code}", "project_code": project, "expected_count": 10},
    )
    assert batch.status_code == 201, batch.text
    payload = {
        "dossier_code": code,
        "intake_id": batch.json()["id"],
        "asset_type": "工艺技术文档",
        "quantity": 10,
        "unit": "份",
        "vault_id": vault["id"],
        "secrecy_level": secrecy,
    }
    if retention:
        payload["retention_until"] = retention
    dossier = client.post("/api/dossiers", headers=admin["headers"], json=payload)
    assert dossier.status_code == 201, dossier.text
    return batch.json(), dossier.json()


def create_hold(client, admin, scope, hold_code="LH-2026-001", matter="CASE-2026-001", review_until="2026-10-01"):
    response = client.post(
        "/api/legal-holds",
        headers=admin["headers"],
        json={
            "hold_code": hold_code,
            "matter_reference": matter,
            "notice_type": "litigation",
            "reason": "收到法院诉讼保全通知",
            "scope": scope,
            "review_until": review_until,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def approved_disposal_request(client, admin, approvers, dossier_id, quantity=None):
    payload = {"quantity": quantity} if quantity is not None else {}
    approval = client.post(
        "/api/dossiers/approvals",
        headers=admin["headers"],
        json={"action_type": "disposal", "resource_type": "dossier", "resource_id": dossier_id, "payload": payload},
    )
    assert approval.status_code == 201, approval.text
    request_id = approval.json()["id"]
    for approver in approvers:
        decision = client.post(
            f"/api/dossiers/approvals/{request_id}/decisions",
            headers=approver["headers"],
            json={"decision": "approve", "comment": "同意处置"},
        )
        assert decision.status_code == 200, decision.text
    return request_id


def test_hold_by_project_freezes_scope_and_blocks_disposal(client, admin, approvers):
    _, held_one = create_dossier(client, admin, "LH-A-001", project="P-LIT")
    _, held_two = create_dossier(client, admin, "LH-A-002", project="P-LIT")
    _, other = create_dossier(client, admin, "LH-A-003", project="P-OTHER")

    hold = create_hold(client, admin, {"type": "project", "key": "P-LIT"})
    assert hold["status"] == "active"
    assert hold["scope_type"] == "project"
    assert hold["scope_key"] == "P-LIT"
    assert sorted(item["dossier_code"] for item in hold["items"]) == ["LH-A-001", "LH-A-002"]

    # 重复提交同一保全编号：幂等回放，不产生重复冻结
    replay = create_hold(client, admin, {"type": "project", "key": "P-LIT"})
    assert replay["id"] == hold["id"]
    assert replay["replayed"] is True

    # 相同案件与范围的保全不允许重复冻结
    duplicate = client.post(
        "/api/legal-holds",
        headers=admin["headers"],
        json={
            "hold_code": "LH-2026-002",
            "matter_reference": "CASE-2026-001",
            "notice_type": "litigation",
            "reason": "重复登记",
            "scope": {"type": "project", "key": "P-LIT"},
            "review_until": "2026-10-01",
        },
    )
    assert duplicate.status_code == 409

    # 冻结来源可查询
    sources = client.get(f"/api/legal-holds/by-dossier/{held_one['id']}", headers=admin["headers"])
    assert sources.status_code == 200
    assert sources.json()[0]["hold_code"] == "LH-2026-001"
    assert sources.json()[0]["matter_reference"] == "CASE-2026-001"
    assert sources.json()[0]["status"] == "active"

    # 保全中的档案即使完成双人审批也禁止处置
    request_id = approved_disposal_request(client, admin, approvers, held_one["id"], quantity=2)
    blocked = client.post(
        f"/api/dossier-operations/disposals/{request_id}",
        headers=admin["headers"],
        json={"method": "粉碎销毁", "witness_one": approvers[0]["id"], "witness_two": approvers[1]["id"]},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["context"]["hold_code"] == "LH-2026-001"

    # 未冻结项目的档案不受影响
    other_request = approved_disposal_request(client, admin, approvers, other["id"], quantity=1)
    allowed = client.post(
        f"/api/dossier-operations/disposals/{other_request}",
        headers=admin["headers"],
        json={"method": "粉碎销毁", "witness_one": approvers[0]["id"], "witness_two": approvers[1]["id"]},
    )
    assert allowed.status_code == 201, allowed.text

    # 解除保全后处置放行
    released = client.post(
        f"/api/legal-holds/{hold['id']}/release",
        headers=admin["headers"],
        json={"reason": "案件和解，法院解除保全"},
    )
    assert released.status_code == 200
    assert released.json()["status"] == "released"
    assert all(item["released_at"] for item in released.json()["items"])
    executed = client.post(
        f"/api/dossier-operations/disposals/{request_id}",
        headers=admin["headers"],
        json={"method": "粉碎销毁", "witness_one": approvers[0]["id"], "witness_two": approvers[1]["id"]},
    )
    assert executed.status_code == 201, executed.text
    assert held_two["id"] != held_one["id"]


def test_hold_by_family_and_incident_scopes(client, admin):
    _, parent = create_dossier(client, admin, "LH-B-001", project="P-FAM")
    copy = client.post(
        f"/api/dossiers/{parent['id']}/issue_copys",
        headers=admin["headers"],
        json={"requested_quantity": 4, "children": [{"dossier_code": "LH-B-001-A", "quantity": 4}]},
    )
    assert copy.status_code == 201, copy.text

    family_hold = create_hold(
        client, admin, {"type": "family", "key": str(parent["id"])}, hold_code="LH-FAM-001", matter="CASE-FAM"
    )
    assert sorted(item["dossier_code"] for item in family_hold["items"]) == ["LH-B-001", "LH-B-001-A"]

    incident = client.post(
        "/api/dossiers/incidents",
        headers=admin["headers"],
        json={
            "dossier_id": parent["id"],
            "incident_type": "疑似泄密",
            "severity": "high",
            "description": "外部渠道出现相同工艺参数",
        },
    )
    assert incident.status_code == 201, incident.text
    incident_hold = create_hold(
        client,
        admin,
        {"type": "incident", "key": str(incident.json()["id"])},
        hold_code="LH-INC-001",
        matter="CASE-INC",
    )
    assert [item["dossier_code"] for item in incident_hold["items"]] == ["LH-B-001"]

    empty = client.post(
        "/api/legal-holds",
        headers=admin["headers"],
        json={
            "matter_reference": "CASE-EMPTY",
            "notice_type": "regulatory_investigation",
            "reason": "监管调查",
            "scope": {"type": "project", "key": "P-NONE"},
            "review_until": "2026-10-01",
        },
    )
    assert empty.status_code == 422


def test_hold_extensions_multiple_and_release_guards(client, admin):
    create_dossier(client, admin, "LH-C-001", project="P-EXT")
    hold = create_hold(client, admin, {"type": "project", "key": "P-EXT"}, hold_code="LH-EXT-001")

    first = client.post(
        f"/api/legal-holds/{hold['id']}/extensions",
        headers=admin["headers"],
        json={"new_review_until": "2026-11-01", "reason": "案件一审未结"},
    )
    assert first.status_code == 201, first.text
    second = client.post(
        f"/api/legal-holds/{hold['id']}/extensions",
        headers=admin["headers"],
        json={"new_review_until": "2026-12-15", "reason": "进入二审程序"},
    )
    assert second.status_code == 201, second.text
    detail = client.get(f"/api/legal-holds/{hold['id']}", headers=admin["headers"])
    assert detail.json()["review_until"] == "2026-12-15"
    extensions = detail.json()["extensions"]
    assert len(extensions) == 2
    assert extensions[0]["previous_review_until"] == "2026-10-01"
    assert extensions[0]["new_review_until"] == "2026-11-01"
    assert extensions[1]["new_review_until"] == "2026-12-15"

    backwards = client.post(
        f"/api/legal-holds/{hold['id']}/extensions",
        headers=admin["headers"],
        json={"new_review_until": "2026-11-01", "reason": "错误的回退"},
    )
    assert backwards.status_code == 422

    released = client.post(
        f"/api/legal-holds/{hold['id']}/release",
        headers=admin["headers"],
        json={"reason": "监管调查结束"},
    )
    assert released.status_code == 200
    again = client.post(
        f"/api/legal-holds/{hold['id']}/release",
        headers=admin["headers"],
        json={"reason": "重复解除"},
    )
    assert again.status_code == 409
    extend_after_release = client.post(
        f"/api/legal-holds/{hold['id']}/extensions",
        headers=admin["headers"],
        json={"new_review_until": "2027-01-01", "reason": "已解除"},
    )
    assert extend_after_release.status_code == 409


def test_retention_evaluation_marks_held_blocked(client, admin):
    _, held = create_dossier(client, admin, "LH-D-001", project="P-EXP", retention="2020-01-01")
    _, free = create_dossier(client, admin, "LH-D-002", project="P-EXP-FREE", retention="2020-06-30")
    create_dossier(client, admin, "LH-D-003", project="P-EXP-FUTURE", retention="2099-01-01")
    hold = create_hold(client, admin, {"type": "project", "key": "P-EXP"}, hold_code="LH-EXP-001")

    evaluation = client.get("/api/legal-holds/retention-evaluation", headers=admin["headers"])
    assert evaluation.status_code == 200, evaluation.text
    body = evaluation.json()
    by_code = {item["dossier_code"]: item for item in body["items"]}
    assert set(by_code) == {"LH-D-001", "LH-D-002"}
    assert by_code["LH-D-001"]["evaluation"] == "blocked_by_hold"
    assert by_code["LH-D-001"]["active_hold_codes"] == ["LH-EXP-001"]
    assert by_code["LH-D-002"]["evaluation"] == "eligible_for_disposal_plan"
    assert body["blocked_by_hold_count"] == 1
    assert body["eligible_count"] == 1

    # 解除保全后到期载体转为可评估处置
    client.post(
        f"/api/legal-holds/{hold['id']}/release",
        headers=admin["headers"],
        json={"reason": "诉讼终结"},
    )
    after = client.get("/api/legal-holds/retention-evaluation", headers=admin["headers"])
    assert after.json()["blocked_by_hold_count"] == 0
    assert after.json()["eligible_count"] == 2


def test_disposal_plan_lifecycle_and_verification(client, admin, approvers):
    _, dossier = create_dossier(client, admin, "LH-E-001", project="P-PLAN")
    hold = create_hold(client, admin, {"type": "project", "key": "P-PLAN"}, hold_code="LH-PLAN-001")
    request_id = approved_disposal_request(client, admin, approvers, dossier["id"], quantity=10)
    # 保全期间不得生成处置计划
    blocked = client.post(
        "/api/disposal-plans",
        headers=admin["headers"],
        json={"dossier_id": dossier["id"], "approval_request_id": request_id},
    )
    assert blocked.status_code == 409
    eligibility = blocked.json()["error"]["context"]["eligibility"]
    assert eligibility["hold_clear"] is False
    assert eligibility["active_hold_codes"] == ["LH-PLAN-001"]
    assert eligibility["dual_review_satisfied"] is True

    client.post(f"/api/legal-holds/{hold['id']}/release", headers=admin["headers"], json={"reason": "案件撤诉"})

    plan = client.post(
        "/api/disposal-plans",
        headers=admin["headers"],
        json={"dossier_id": dossier["id"], "approval_request_id": request_id, "plan_code": "PLN-E-001"},
    )
    assert plan.status_code == 201, plan.text
    plan = plan.json()
    assert plan["state"] == "ready"
    snapshot = plan["eligibility"]
    assert snapshot["eligible"] is True
    assert snapshot["hold_clear"] is True
    assert snapshot["dual_review_satisfied"] is True
    assert snapshot["approver_user_ids"] == sorted(user["id"] for user in approvers)
    assert snapshot["dossier_version"] == dossier["version"]
    assert plan["eligibility_digest"]
    assert plan["events"][0]["event_type"] == "created"

    # 同一档案不允许并行的处置计划
    duplicate = client.post(
        "/api/disposal-plans",
        headers=admin["headers"],
        json={"dossier_id": dossier["id"], "approval_request_id": request_id},
    )
    assert duplicate.status_code == 409

    executed = client.post(
        f"/api/disposal-plans/{plan['id']}/execute",
        headers=admin["headers"],
        json={"method": "粉碎销毁", "witness_one": approvers[0]["id"], "witness_two": approvers[1]["id"]},
    )
    assert executed.status_code == 200, executed.text
    assert executed.json()["plan"]["state"] == "executed"
    assert executed.json()["disposal"]["record"]["disposed_quantity"] == 10

    verification = client.get(f"/api/disposal-plans/{plan['id']}/verification", headers=admin["headers"])
    assert verification.status_code == 200
    result = verification.json()
    assert result["digest_match"] is True
    assert result["recomputed_digest"] == result["certificate_digest"]
    assert result["dossier"]["dossier_code"] == "LH-E-001"
    assert result["dossier"]["lifecycle_state"] == "disposed"
    assert [w["id"] for w in result["witnesses"]] == [approvers[0]["id"], approvers[1]["id"]]

    # 已处置档案不能再生成处置计划
    disposed = client.post(
        "/api/disposal-plans",
        headers=admin["headers"],
        json={"dossier_id": dossier["id"], "approval_request_id": request_id},
    )
    assert disposed.status_code == 409


def test_plan_requires_dual_review_and_matching_approval(client, admin, approvers):
    _, dossier = create_dossier(client, admin, "LH-F-001", project="P-DR")
    approval = client.post(
        "/api/dossiers/approvals",
        headers=admin["headers"],
        json={"action_type": "disposal", "resource_type": "dossier", "resource_id": dossier["id"], "payload": {}},
    )
    request_id = approval.json()["id"]

    pending = client.post(
        "/api/disposal-plans",
        headers=admin["headers"],
        json={"dossier_id": dossier["id"], "approval_request_id": request_id},
    )
    assert pending.status_code == 409
    assert pending.json()["error"]["context"]["eligibility"]["dual_review_satisfied"] is False

    client.post(
        f"/api/dossiers/approvals/{request_id}/decisions",
        headers=approvers[0]["headers"],
        json={"decision": "approve"},
    )
    one_vote = client.post(
        "/api/disposal-plans",
        headers=admin["headers"],
        json={"dossier_id": dossier["id"], "approval_request_id": request_id},
    )
    assert one_vote.status_code == 409

    client.post(
        f"/api/dossiers/approvals/{request_id}/decisions",
        headers=approvers[1]["headers"],
        json={"decision": "approve"},
    )
    ready = client.post(
        "/api/disposal-plans",
        headers=admin["headers"],
        json={"dossier_id": dossier["id"], "approval_request_id": request_id},
    )
    assert ready.status_code == 201, ready.text

    _, other = create_dossier(client, admin, "LH-F-002", project="P-DR-2")
    mismatched = client.post(
        "/api/disposal-plans",
        headers=admin["headers"],
        json={"dossier_id": other["id"], "approval_request_id": request_id},
    )
    assert mismatched.status_code == 422


def test_top_secret_dossier_cannot_enter_plan(client, admin, approvers):
    _, dossier = create_dossier(client, admin, "LH-G-001", project="P-TS", secrecy="top_secret")
    request_id = approved_disposal_request(client, admin, approvers, dossier["id"], quantity=1)
    response = client.post(
        "/api/disposal-plans",
        headers=admin["headers"],
        json={"dossier_id": dossier["id"], "approval_request_id": request_id},
    )
    assert response.status_code == 409
    eligibility = response.json()["error"]["context"]["eligibility"]
    assert eligibility["secrecy_satisfied"] is False
    assert eligibility["secrecy_level"] == "top_secret"


def test_plan_auto_pauses_when_hold_reactivates(client, admin, approvers):
    _, dossier = create_dossier(client, admin, "LH-H-001", project="P-PAUSE")
    request_id = approved_disposal_request(client, admin, approvers, dossier["id"], quantity=1)
    plan = client.post(
        "/api/disposal-plans",
        headers=admin["headers"],
        json={"dossier_id": dossier["id"], "approval_request_id": request_id},
    )
    assert plan.status_code == 201
    plan_id = plan.json()["id"]

    hold = create_hold(client, admin, {"type": "project", "key": "P-PAUSE"}, hold_code="LH-PAUSE-001", matter="CASE-PAUSE")

    detail = client.get(f"/api/disposal-plans/{plan_id}", headers=admin["headers"])
    assert detail.json()["state"] == "paused"
    assert detail.json()["pause_reason"] == "legal_hold_activated"
    event_types = [event["event_type"] for event in detail.json()["events"]]
    assert event_types == ["created", "paused"]

    execute = client.post(
        f"/api/disposal-plans/{plan_id}/execute",
        headers=admin["headers"],
        json={"method": "粉碎销毁", "witness_one": approvers[0]["id"], "witness_two": approvers[1]["id"]},
    )
    assert execute.status_code == 409

    # 解除保全后暂停的计划不会自动恢复，需作废后重新评估生成
    client.post(f"/api/legal-holds/{hold['id']}/release", headers=admin["headers"], json={"reason": "保全期满"})
    still_paused = client.get(f"/api/disposal-plans/{plan_id}", headers=admin["headers"])
    assert still_paused.json()["state"] == "paused"

    cancelled = client.post(
        f"/api/disposal-plans/{plan_id}/cancel",
        headers=admin["headers"],
        json={"reason": "保全解除后重新评估"},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"

    replacement = client.post(
        "/api/disposal-plans",
        headers=admin["headers"],
        json={"dossier_id": dossier["id"], "approval_request_id": request_id},
    )
    assert replacement.status_code == 201, replacement.text
    plans = client.get(f"/api/disposal-plans?dossier_id={dossier['id']}", headers=admin["headers"])
    assert sorted(item["state"] for item in plans.json()) == ["cancelled", "ready"]


def test_plan_auto_pauses_on_dossier_version_change(client, admin, approvers):
    _, dossier = create_dossier(client, admin, "LH-I-001", project="P-VER")
    request_id = approved_disposal_request(client, admin, approvers, dossier["id"], quantity=1)
    plan = client.post(
        "/api/disposal-plans",
        headers=admin["headers"],
        json={"dossier_id": dossier["id"], "approval_request_id": request_id},
    )
    plan_id = plan.json()["id"]

    target = create_vault(client, admin, "V-TRANSFER")
    moved = client.post(
        f"/api/dossier-operations/{dossier['id']}/transfers",
        headers=admin["headers"],
        json={"vault_id": target["id"], "expected_version": dossier["version"], "reason": "调整库位"},
    )
    assert moved.status_code == 200, moved.text

    detail = client.get(f"/api/disposal-plans/{plan_id}", headers=admin["headers"])
    assert detail.json()["state"] == "paused"
    assert detail.json()["pause_reason"] == "dossier_version_changed"
    paused_event = detail.json()["events"][-1]
    assert paused_event["event_type"] == "paused"
    assert paused_event["details"]["plan_dossier_version"] == dossier["version"]

    execute = client.post(
        f"/api/disposal-plans/{plan_id}/execute",
        headers=admin["headers"],
        json={"method": "粉碎销毁", "witness_one": approvers[0]["id"], "witness_two": approvers[1]["id"]},
    )
    assert execute.status_code == 409


def test_plan_history_is_append_only(client, admin, approvers):
    _, dossier = create_dossier(client, admin, "LH-J-001", project="P-HIST")
    request_id = approved_disposal_request(client, admin, approvers, dossier["id"], quantity=1)
    plan = client.post(
        "/api/disposal-plans",
        headers=admin["headers"],
        json={"dossier_id": dossier["id"], "approval_request_id": request_id},
    )
    plan_id = plan.json()["id"]
    original_digest = plan.json()["eligibility_digest"]
    original_eligibility = plan.json()["eligibility"]

    client.post(
        f"/api/disposal-plans/{plan_id}/cancel",
        headers=admin["headers"],
        json={"reason": "处置窗口调整"},
    )
    detail = client.get(f"/api/disposal-plans/{plan_id}", headers=admin["headers"])
    assert [event["event_type"] for event in detail.json()["events"]] == ["created", "cancelled"]
    assert detail.json()["eligibility_digest"] == original_digest
    assert detail.json()["eligibility"] == original_eligibility

    cancel_again = client.post(
        f"/api/disposal-plans/{plan_id}/cancel",
        headers=admin["headers"],
        json={"reason": "重复作废"},
    )
    assert cancel_again.status_code == 409


def test_hold_permissions_enforced(client, admin):
    create_dossier(client, admin, "LH-K-001", project="P-PERM")
    researcher = create_user(client, admin, "researcher.only", ["researcher"])
    denied = client.post(
        "/api/legal-holds",
        headers=researcher["headers"],
        json={
            "matter_reference": "CASE-PERM",
            "notice_type": "litigation",
            "reason": "越权尝试",
            "scope": {"type": "project", "key": "P-PERM"},
            "review_until": "2026-10-01",
        },
    )
    assert denied.status_code == 403

    legal = create_user(client, admin, "legal.officer", ["legal_officer"])
    allowed = client.post(
        "/api/legal-holds",
        headers=legal["headers"],
        json={
            "matter_reference": "CASE-PERM",
            "notice_type": "litigation",
            "reason": "法务登记保全",
            "scope": {"type": "project", "key": "P-PERM"},
            "review_until": "2026-10-01",
        },
    )
    assert allowed.status_code == 201, allowed.text
    assert allowed.json()["items"][0]["dossier_code"] == "LH-K-001"


def test_legacy_database_gains_secrecy_and_retention_columns(tmp_path, monkeypatch):
    import sqlite3

    from app.database import close_connection, get_connection, init_db

    db_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE dossiers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dossier_code TEXT NOT NULL UNIQUE,
            intake_id INTEGER NOT NULL,
            disclosure_event_id INTEGER,
            source_dossier_id INTEGER,
            root_dossier_id INTEGER,
            asset_type TEXT NOT NULL,
            quantity REAL NOT NULL,
            reserved_quantity REAL NOT NULL DEFAULT 0,
            unit TEXT NOT NULL,
            lifecycle_state TEXT NOT NULL,
            vault_id INTEGER,
            custody_user_id INTEGER,
            provenance_depth INTEGER NOT NULL DEFAULT 0,
            version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    legacy.close()
    monkeypatch.setenv("ARCHIVE_DATABASE_PATH", str(db_path))
    close_connection()
    init_db()
    columns = {row[1] for row in get_connection().execute("PRAGMA table_info(dossiers)")}
    assert {"secrecy_level", "retention_until"} <= columns
    close_connection()
