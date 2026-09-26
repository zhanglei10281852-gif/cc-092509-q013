from __future__ import annotations


def _create_user(client, admin, username, role_code, permissions):
    role = client.post(
        "/api/roles",
        headers=admin["headers"],
        json={"code": role_code, "name": role_code, "permission_codes": permissions},
    )
    assert role.status_code == 201, role.text
    user = client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": username, "password": "Passw0rd!2345", "display_name": username, "role_codes": [role_code]},
    )
    assert user.status_code == 201, user.text
    login = client.post("/api/auth/login", json={"username": username, "password": "Passw0rd!2345", "client_label": "tests"})
    assert login.status_code == 200, login.text
    return {"headers": {"Authorization": f"Bearer {login.json()['token']}"}, "body": login.json()}


def _approvers(client, admin):
    first = _create_user(client, admin, "approver.one", "approver.one", ["approvals.decide", "dossiers.read"])
    second = _create_user(client, admin, "approver.two", "approver.two", ["approvals.decide", "dossiers.read"])
    return first, second


def _make_dossier(client, admin, code, *, project="P-HOLD", secrecy="confidential", retention="2020-01-01", quantity=10):
    vault = client.post(
        "/api/dossiers/vaults",
        headers=admin["headers"],
        json={
            "code": f"V-{code}",
            "building": "档案楼",
            "room": "常温库",
            "cabinet": "一号柜",
            "shelf": "一层",
            "sensitivity": "normal",
            "capacity_units": 100,
        },
    )
    assert vault.status_code == 201, vault.text
    batch = client.post(
        "/api/dossiers/batches",
        headers=admin["headers"],
        json={"intake_code": f"BATCH-{code}", "project_code": project, "expected_count": 1},
    )
    assert batch.status_code == 201, batch.text
    payload = {
        "dossier_code": code,
        "intake_id": batch.json()["id"],
        "asset_type": "专利交底书",
        "quantity": quantity,
        "unit": "份",
        "vault_id": vault.json()["id"],
        "secrecy_level": secrecy,
    }
    if retention is not None:
        payload["retention_until"] = retention
    dossier = client.post("/api/dossiers", headers=admin["headers"], json=payload)
    assert dossier.status_code == 201, dossier.text
    return dossier.json()


def _approved_disposal(client, admin, approver_one, approver_two, dossier_id, quantity):
    approval = client.post(
        "/api/dossiers/approvals",
        headers=admin["headers"],
        json={"action_type": "disposal", "resource_type": "dossier", "resource_id": dossier_id, "payload": {"quantity": quantity}},
    )
    assert approval.status_code == 201, approval.text
    request_id = approval.json()["id"]
    for approver in (approver_one, approver_two):
        decision = client.post(
            f"/api/dossiers/approvals/{request_id}/decisions",
            headers=approver["headers"],
            json={"decision": "approve", "comment": "同意"},
        )
        assert decision.status_code == 200, decision.text
    return approval.json()


def _create_plan(client, admin, dossier, approval_id, quantity, *, plan_code=None, method="粉碎销毁"):
    payload = {
        "dossier_id": dossier["id"],
        "approval_request_id": approval_id,
        "planned_quantity": quantity,
        "method": method,
    }
    if plan_code:
        payload["plan_code"] = plan_code
    return client.post("/api/disposal-plans", headers=admin["headers"], json=payload)


def test_legal_hold_freezes_project_scope_and_replays(client, admin):
    first = _make_dossier(client, admin, "HLD-A1")
    second = _make_dossier(client, admin, "HLD-A2")
    payload = {
        "hold_code": "LH-CASE-001",
        "scope_type": "project",
        "scope_key": "P-HOLD",
        "source_type": "litigation",
        "source_reference": "(2026) 京 73 民初 100 号",
        "reason": "收到法院诉讼通知，冻结相关专利档案",
        "review_by": "2027-03-01T00:00:00+00:00",
    }
    created = client.post("/api/legal-holds", headers=admin["headers"], json=payload)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["replayed"] is False
    assert sorted(body["attached"]) == ["HLD-A1", "HLD-A2"]
    hold = body["hold"]
    assert hold["source_type"] == "litigation"
    assert hold["source_reference"] == "(2026) 京 73 民初 100 号"
    assert hold["scope_type"] == "project"
    assert hold["scope_key"] == "P-HOLD"
    assert hold["state"] == "active"
    assert {item["dossier_code"] for item in hold["items"]} == {"HLD-A1", "HLD-A2"}
    assert hold["events"][0]["event_type"] == "created"

    replayed = client.post("/api/legal-holds", headers=admin["headers"], json=payload)
    assert replayed.status_code == 201
    assert replayed.json()["replayed"] is True
    assert replayed.json()["hold"]["id"] == hold["id"]

    conflict = client.post("/api/legal-holds", headers=admin["headers"], json={**payload, "reason": "另一份通知"})
    assert conflict.status_code == 409

    detail = client.get(f"/api/legal-holds/{hold['id']}", headers=admin["headers"])
    assert detail.status_code == 200
    assert detail.json()["item_count"] == 2
    assert first["id"] != second["id"]


def test_overlapping_holds_skip_already_frozen_dossiers(client, admin):
    _make_dossier(client, admin, "HLD-B1")
    _make_dossier(client, admin, "HLD-B2")
    first = client.post(
        "/api/legal-holds",
        headers=admin["headers"],
        json={
            "scope_type": "project",
            "scope_key": "P-HOLD",
            "source_type": "litigation",
            "source_reference": "案件一",
            "reason": "第一起诉讼冻结",
            "review_by": "2027-01-01",
        },
    )
    assert first.status_code == 201, first.text
    second = client.post(
        "/api/legal-holds",
        headers=admin["headers"],
        json={
            "scope_type": "project",
            "scope_key": "P-HOLD",
            "source_type": "regulatory_investigation",
            "source_reference": "监管调查函 2026-88",
            "reason": "监管调查需要重复冻结同一批档案",
            "review_by": "2027-06-01",
        },
    )
    assert second.status_code == 201, second.text
    body = second.json()
    assert body["attached"] == []
    assert len(body["skipped_already_held"]) == 2
    assert body["hold"]["item_count"] == 0


def test_hold_extensions_release_and_reactivate(client, admin):
    _make_dossier(client, admin, "HLD-C1")
    hold = client.post(
        "/api/legal-holds",
        headers=admin["headers"],
        json={
            "scope_type": "project",
            "scope_key": "P-HOLD",
            "source_type": "litigation",
            "source_reference": "案件二",
            "reason": "诉讼通知冻结",
            "review_by": "2027-01-01",
        },
    ).json()["hold"]

    backwards = client.post(
        f"/api/legal-holds/{hold['id']}/extensions",
        headers=admin["headers"],
        json={"new_review_by": "2026-06-01", "reason": "试图缩短期限"},
    )
    assert backwards.status_code == 422

    for new_date in ("2027-06-01", "2027-12-01", "2028-06-01"):
        extended = client.post(
            f"/api/legal-holds/{hold['id']}/extensions",
            headers=admin["headers"],
            json={"new_review_by": new_date, "reason": "案件审理延期"},
        )
        assert extended.status_code == 200, extended.text
    detail = client.get(f"/api/legal-holds/{hold['id']}", headers=admin["headers"]).json()
    assert detail["review_by"].startswith("2028-06-01")
    assert len(detail["extensions"]) == 3
    assert [event["event_type"] for event in detail["events"]].count("extended") == 3

    released = client.post(
        f"/api/legal-holds/{hold['id']}/release",
        headers=admin["headers"],
        json={"reason": "案件和解，解除保全"},
    )
    assert released.status_code == 200
    assert released.json()["state"] == "released"
    again = client.post(
        f"/api/legal-holds/{hold['id']}/release",
        headers=admin["headers"],
        json={"reason": "重复解除"},
    )
    assert again.status_code == 409
    extend_released = client.post(
        f"/api/legal-holds/{hold['id']}/extensions",
        headers=admin["headers"],
        json={"new_review_by": "2029-01-01", "reason": "已解除不能延期"},
    )
    assert extend_released.status_code == 409

    reactivated = client.post(
        f"/api/legal-holds/{hold['id']}/reactivate",
        headers=admin["headers"],
        json={"review_by": "2029-01-01"},
    )
    assert reactivated.status_code == 200, reactivated.text
    assert reactivated.json()["state"] == "active"
    assert reactivated.json()["item_count"] == 1
    event_types = [event["event_type"] for event in reactivated.json()["events"]]
    assert "released" in event_types and "reactivated" in event_types


def test_retention_assessment_blocks_held_and_allows_released(client, admin):
    held_dossier = _make_dossier(client, admin, "RET-A1", retention="2020-01-01")
    fresh_dossier = _make_dossier(client, admin, "RET-A2", retention="2099-01-01")
    hold = client.post(
        "/api/legal-holds",
        headers=admin["headers"],
        json={
            "hold_code": "LH-RET-1",
            "scope_type": "family",
            "scope_key": "RET-A1",
            "source_type": "regulatory_investigation",
            "source_reference": "监管函 1 号",
            "reason": "调查期间冻结",
            "review_by": "2027-01-01",
        },
    ).json()["hold"]

    held = client.post("/api/retention/assessments", headers=admin["headers"], json={"dossier_id": held_dossier["id"]})
    assert held.status_code == 201, held.text
    assert held.json()["outcome"] == "held"
    assert held.json()["active_hold_count"] == 1

    not_due = client.post("/api/retention/assessments", headers=admin["headers"], json={"dossier_id": fresh_dossier["id"]})
    assert not_due.json()["outcome"] == "not_due"

    sweep = client.post("/api/retention/assess-due", headers=admin["headers"], json={"as_of": "2026-09-26"})
    assert sweep.status_code == 200
    assert sweep.json()["summary"] == {"not_due": 0, "held": 1, "eligible": 0}

    client.post(f"/api/legal-holds/{hold['id']}/release", headers=admin["headers"], json={"reason": "调查结束"})
    eligible = client.post("/api/retention/assessments", headers=admin["headers"], json={"dossier_id": held_dossier["id"]})
    assert eligible.json()["outcome"] == "eligible"

    history = client.get(f"/api/retention/assessments?dossier_id={held_dossier['id']}", headers=admin["headers"])
    outcomes = [item["outcome"] for item in history.json()]
    # 逐条评估与批量评估各留一条 held 记录，解除保全后最新一条为 eligible
    assert outcomes[0] == "eligible"
    assert set(outcomes[1:]) == {"held"}


def test_disposal_plan_requires_secrecy_and_dual_review(client, admin):
    approver_one, approver_two = _approvers(client, admin)
    dossier = _make_dossier(client, admin, "PLAN-A1", retention="2020-01-01")

    pending = client.post(
        "/api/dossiers/approvals",
        headers=admin["headers"],
        json={"action_type": "disposal", "resource_type": "dossier", "resource_id": dossier["id"], "payload": {"quantity": 5}},
    ).json()
    refused = _create_plan(client, admin, dossier, pending["id"], 5)
    assert refused.status_code == 409
    assert "dual_review_approved" in refused.json()["error"]["context"]["failed_checks"]

    approval = _approved_disposal(client, admin, approver_one, approver_two, dossier["id"], 5)
    top_secret = _make_dossier(client, admin, "PLAN-A2", secrecy="top_secret", retention="2020-01-01")
    approval_two = _approved_disposal(client, admin, approver_one, approver_two, top_secret["id"], 5)
    blocked = _create_plan(client, admin, top_secret, approval_two["id"], 5)
    assert blocked.status_code == 409
    assert "secrecy_level_disposable" in blocked.json()["error"]["context"]["failed_checks"]

    created = _create_plan(client, admin, dossier, approval["id"], 5, plan_code="DP-0001")
    assert created.status_code == 201, created.text
    plan = created.json()["plan"]
    assert plan["state"] == "scheduled"
    assert plan["dossier_version"] == dossier["version"]
    eligibility = plan["eligibility"]
    assert eligibility["eligible"] is True
    assert eligibility["judged_at"]
    assert len(eligibility["approver_user_ids"]) == 2
    assert all(check["passed"] for check in eligibility["checks"])

    replayed = _create_plan(client, admin, dossier, approval["id"], 5, plan_code="DP-0001")
    assert replayed.status_code == 201
    assert replayed.json()["replayed"] is True

    duplicate = _create_plan(client, admin, dossier, approval["id"], 5)
    assert duplicate.status_code == 409


def test_plan_auto_pauses_on_hold_and_version_change(client, admin):
    approver_one, approver_two = _approvers(client, admin)
    dossier = _make_dossier(client, admin, "PAUSE-A1", retention="2020-01-01", quantity=20)
    approval = _approved_disposal(client, admin, approver_one, approver_two, dossier["id"], 5)
    plan = _create_plan(client, admin, dossier, approval["id"], 5).json()["plan"]

    hold = client.post(
        "/api/legal-holds",
        headers=admin["headers"],
        json={
            "scope_type": "family",
            "scope_key": "PAUSE-A1",
            "source_type": "litigation",
            "source_reference": "案件三",
            "reason": "诉讼冻结",
            "review_by": "2027-01-01",
        },
    ).json()["hold"]
    paused = client.get(f"/api/disposal-plans/{plan['id']}", headers=admin["headers"]).json()
    assert paused["state"] == "paused"
    assert paused["pause_reason"] == "legal_hold_activated"

    client.post(f"/api/legal-holds/{hold['id']}/release", headers=admin["headers"], json={"reason": "诉讼结案解除保全"})
    resumed = client.post(f"/api/disposal-plans/{plan['id']}/resume", headers=admin["headers"])
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["state"] == "scheduled"

    # 档案版本变化（签发受控副本）后自动暂停，且不允许恢复
    changed = client.post(
        f"/api/dossiers/{dossier['id']}/issue_copys",
        headers=admin["headers"],
        json={"requested_quantity": 2, "children": [{"dossier_code": "PAUSE-A1-C", "quantity": 2}]},
    )
    assert changed.status_code == 201, changed.text
    paused_again = client.get(f"/api/disposal-plans/{plan['id']}", headers=admin["headers"]).json()
    assert paused_again["state"] == "paused"
    assert paused_again["pause_reason"] == "dossier_version_changed"
    resume_refused = client.post(f"/api/disposal-plans/{plan['id']}/resume", headers=admin["headers"])
    assert resume_refused.status_code == 409

    cancelled = client.post(
        f"/api/disposal-plans/{plan['id']}/cancel",
        headers=admin["headers"],
        json={"reason": "版本变化，作废后重新评估"},
    )
    assert cancelled.status_code == 200
    again = client.post(
        f"/api/disposal-plans/{plan['id']}/cancel",
        headers=admin["headers"],
        json={"reason": "重复作废"},
    )
    assert again.status_code == 409


def test_plan_execution_and_post_execution_verification(client, admin):
    approver_one, approver_two = _approvers(client, admin)
    witness_one = _create_user(client, admin, "witness.one", "witness.one", ["dossiers.read"])
    witness_two = _create_user(client, admin, "witness.two", "witness.two", ["dossiers.read"])
    dossier = _make_dossier(client, admin, "EXEC-A1", retention="2020-01-01", quantity=10)
    approval = _approved_disposal(client, admin, approver_one, approver_two, dossier["id"], 4)
    plan = _create_plan(client, admin, dossier, approval["id"], 4).json()["plan"]

    witness_one_id = witness_one["body"]["user"]["id"]
    witness_two_id = witness_two["body"]["user"]["id"]
    executed = client.post(
        f"/api/disposal-plans/{plan['id']}/execute",
        headers=admin["headers"],
        json={"witness_one": witness_one_id, "witness_two": witness_two_id},
    )
    assert executed.status_code == 201, executed.text
    assert executed.json()["plan"]["state"] == "executed"

    verification = client.get(f"/api/disposal-plans/{plan['id']}/verification", headers=admin["headers"])
    assert verification.status_code == 200, verification.text
    body = verification.json()
    assert body["digest_valid"] is True
    assert body["actual_dossier"]["dossier_code"] == "EXEC-A1"
    assert body["disposal_record"]["disposed_quantity"] == 4
    assert body["witnesses"]["witness_one"]["user_id"] == witness_one_id
    assert body["witnesses"]["witness_two"]["user_id"] == witness_two_id
    assert len(body["certificate_digest"]) == 64

    # 历史计划与决定不能被覆盖
    repeat = client.post(
        f"/api/disposal-plans/{plan['id']}/execute",
        headers=admin["headers"],
        json={"witness_one": witness_one_id, "witness_two": witness_two_id},
    )
    assert repeat.status_code == 409
    cancel = client.post(
        f"/api/disposal-plans/{plan['id']}/cancel",
        headers=admin["headers"],
        json={"reason": "已执行不能作废"},
    )
    assert cancel.status_code == 409
    events = [event["event_type"] for event in body["plan"]["events"]]
    assert events == ["created", "executed"]


def test_active_hold_blocks_direct_disposal(client, admin):
    approver_one, approver_two = _approvers(client, admin)
    witness_one = _create_user(client, admin, "witness.three", "witness.three", ["dossiers.read"])
    witness_two = _create_user(client, admin, "witness.four", "witness.four", ["dossiers.read"])
    dossier = _make_dossier(client, admin, "DIR-A1", retention="2020-01-01")
    approval = _approved_disposal(client, admin, approver_one, approver_two, dossier["id"], 3)
    client.post(
        "/api/legal-holds",
        headers=admin["headers"],
        json={
            "scope_type": "family",
            "scope_key": "DIR-A1",
            "source_type": "litigation",
            "source_reference": "案件四",
            "reason": "诉讼冻结",
            "review_by": "2027-01-01",
        },
    )
    blocked = client.post(
        f"/api/dossier-operations/disposals/{approval['id']}",
        headers=admin["headers"],
        json={
            "method": "粉碎销毁",
            "witness_one": witness_one["body"]["user"]["id"],
            "witness_two": witness_two["body"]["user"]["id"],
        },
    )
    assert blocked.status_code == 409
    assert "法律保全" in blocked.json()["error"]["message"]


def test_hold_by_incident_scope_and_permissions(client, admin):
    dossier = _make_dossier(client, admin, "INC-A1")
    incident = client.post(
        "/api/dossiers/incidents",
        headers=admin["headers"],
        json={"dossier_id": dossier["id"], "incident_type": "疑似泄密", "severity": "high", "description": "载体去向不明"},
    )
    assert incident.status_code == 201, incident.text
    case_code = incident.json()["case_code"]
    created = client.post(
        "/api/legal-holds",
        headers=admin["headers"],
        json={
            "scope_type": "incident",
            "scope_key": case_code,
            "source_type": "regulatory_investigation",
            "source_reference": "监管调查函 2026-99",
            "reason": "泄密事件调查冻结",
            "review_by": "2027-01-01",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["attached"] == ["INC-A1"]

    researcher = _create_user(client, admin, "researcher.two", "researcher.two", ["dossiers.read"])
    denied = client.post(
        "/api/legal-holds",
        headers=researcher["headers"],
        json={
            "scope_type": "project",
            "scope_key": "P-HOLD",
            "source_type": "litigation",
            "source_reference": "无权操作",
            "reason": "普通研究员不能冻结",
            "review_by": "2027-01-01",
        },
    )
    assert denied.status_code == 403
