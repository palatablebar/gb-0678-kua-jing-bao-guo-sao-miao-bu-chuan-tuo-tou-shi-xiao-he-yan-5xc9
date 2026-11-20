import tempfile
from pathlib import Path

import pytest

from app import store, verification
from app.errors import DomainError
from app.modules.m8_verifications import rules
from app.pipeline import execute

HEADER = "scan_id,parcel,node,event_time,received_time,destination,promised_hours,declared_value,reported,note\n"


def csv(*rows):
    return (HEADER + "".join(row + "\n" for row in rows)).encode()


ACCEPT = "0010,0009,收件,2026-11-02T00:00:00Z,2026-11-02T01:00:00Z,JP,24,200,0,集运"
DELIVERED_ON_TIME = "0013,0009,妥投,2026-11-04T00:00:00Z,2026-11-06T00:00:00Z,JP,24,200,0,补传"
DELIVERED_LATE = "0013,0009,妥投,2026-11-10T00:00:00Z,2026-11-11T00:00:00Z,JP,24,200,0,补传"


def only(run):
    decisions = run["verification"]["decisions"]
    assert len(decisions) == 1
    return decisions[0]


def test_first_scan_creates_single_task():
    run = execute(csv(ACCEPT))
    decision = only(run)
    assert decision["action"] == "create"
    assert decision["owner"] == rules.DEFAULT_POLICY["default_owner"]
    assert decision["role"] == "客服"
    assert decision["revision"] == 1
    assert verification.tasks()[0]["deadline"] == decision["deadline"]


def test_conclusion_change_hands_off_to_dispute_owner():
    execute(csv(ACCEPT))
    run = execute(csv(ACCEPT, DELIVERED_ON_TIME))
    decision = only(run)
    assert decision["action"] == "handoff"
    assert decision["owner"] == rules.DEFAULT_POLICY["dispute_owner"]
    assert decision["role"] == "争议处置"
    assert decision["conclusion_before"] == "超时" and decision["conclusion_after"] == "达标"
    assert decision["conclusion_changed"] is True
    assert "补传" in decision["handoff_reason"]


def test_unchanged_conclusion_keeps_original_owner_and_deadline():
    execute(csv(ACCEPT))
    first = verification.tasks()[0]
    run = execute(csv(ACCEPT, "0011,0009,起运,2026-11-02T12:00:00Z,2026-11-02T13:00:00Z,JP,24,200,0,补传"))
    decision = only(run)
    assert decision["action"] == "continue"
    assert decision["owner"] == first["owner"]
    assert decision["deadline"] == first["deadline"]


def test_same_retry_is_idempotent():
    execute(csv(ACCEPT))
    execute(csv(ACCEPT, DELIVERED_ON_TIME))
    before = verification.tasks(include_inactive=True)
    run = execute(csv(ACCEPT, DELIVERED_ON_TIME))
    assert only(run)["action"] == "noop"
    assert verification.tasks(include_inactive=True) == before
    assert len(verification.tasks()) == 1


def test_retry_after_review_does_not_create_task():
    execute(csv(ACCEPT, DELIVERED_ON_TIME))
    active = verification.tasks()[0]
    verification.review(active["task_id"], active["version"], "已核验")
    run = execute(csv(ACCEPT, DELIVERED_ON_TIME))
    assert only(run)["action"] == "noop"
    assert verification.tasks()[0]["status"] == "已核验"


def test_one_active_task_per_parcel_across_revisions():
    execute(csv(ACCEPT))
    execute(csv(ACCEPT, DELIVERED_ON_TIME))
    execute(csv(ACCEPT, DELIVERED_LATE))
    active = verification.tasks()
    assert len(active) == 1 and active[0]["revision"] == 3
    assert active[0]["owner"] == rules.DEFAULT_POLICY["dispute_owner"]
    history = verification.tasks(include_inactive=True)
    assert [task["revision"] for task in history] == [1, 2, 3]
    assert [task["active"] for task in history] == [False, False, True]


def test_superseded_task_keeps_evidence_and_reason():
    execute(csv(ACCEPT))
    first = verification.tasks()[0]
    execute(csv(ACCEPT, DELIVERED_ON_TIME))
    old = verification.tasks(include_inactive=True)[0]
    assert old["task_id"] == first["task_id"]
    assert old["scan_ids"] == first["scan_ids"]
    assert old["handoff_reason"] == first["handoff_reason"]
    assert old["status"] == "已取代" and old["superseded_by"]
    assert old["evidence"] == first["evidence"]


def test_failed_handoff_keeps_previous_task():
    execute(csv(ACCEPT))
    before = verification.tasks(include_inactive=True)
    events_before = verification.recent_events()

    def boom(db, decision):
        raise DomainError("TASK_WRITE_FAILED", "模拟替换失败", 500)

    # 单独的作用域，避免 undo 波及 conftest 的数据库隔离补丁。
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(verification, "_insert", boom)
        with pytest.raises(DomainError):
            execute(csv(ACCEPT, DELIVERED_ON_TIME))
    # 失败批次整批回滚；旧任务、证据与事件保持原样。
    assert verification.tasks(include_inactive=True) == before
    assert verification.recent_events() == events_before


def test_assignment_without_dispute_permission_is_rejected():
    from app.pipeline import calculate
    execute(csv(ACCEPT))
    before = verification.tasks()
    run = calculate(csv(ACCEPT, DELIVERED_ON_TIME))
    result = verification.sync(run, policy={"dispute_owner": "客服A"})
    assert result["decisions"][0]["action"] == "reject"
    assert "不具备争议处置权限" in result["decisions"][0]["handoff_reason"]
    assert verification.tasks() == before


def test_original_scan_records_survive_sync():
    run = execute(csv(ACCEPT))
    execute(csv(ACCEPT, DELIVERED_ON_TIME))
    stored = store.get(run["run_id"])
    assert stored["stages"][0]["rows"][0]["scan_id"] == "0010"
    assert stored["stages"][0]["metrics"]["input"] == 1


def test_deadline_extension_only_when_expired():
    from app.pipeline import calculate
    execute(csv(ACCEPT))
    first = verification.tasks()[0]
    run = execute(csv(ACCEPT, "0011,0009,起运,2026-11-02T12:00:00Z,2026-11-02T13:00:00Z,JP,24,200,0,补传"))
    assert verification.tasks()[0]["deadline"] == first["deadline"]
    assert verification.tasks()[0]["revision"] == 2
    # 同一负责人续办时，原截止时间未过期就保留；过期后才顺延。
    expired = calculate(csv(ACCEPT, "0011,0009,起运,2026-11-02T12:00:00Z,2026-11-02T13:00:00Z,JP,24,200,0,补传",
                             "0012,0009,清关,2026-11-03T00:00:00Z,2026-11-03T01:00:00Z,JP,24,200,0,补传"))
    later = verification.sync(expired, now="2026-11-30T00:00:00Z")
    assert later["decisions"][0]["action"] == "continue"
    assert later["decisions"][0]["owner"] == first["owner"]
    assert later["decisions"][0]["deadline"] > first["deadline"]


def test_api_lists_tasks_and_detail():
    from app.main import create_app
    client = create_app().test_client()
    client.post("/api/runs", json={"sample": "clean.csv"})
    listing = client.get("/api/verifications").get_json()
    assert listing["metrics"]["parcels"] == len(listing["tasks"]) == 8
    parcel = listing["tasks"][0]["parcel"]
    detail = client.get(f"/api/verifications/{parcel}").get_json()
    assert detail["active"]["parcel"] == parcel
    assert detail["events"]


def test_api_sync_is_idempotent():
    from app.main import create_app
    client = create_app().test_client()
    run = client.post("/api/runs", json={"sample": "clean.csv"}).get_json()
    first = client.post("/api/verifications/sync", json={"run_id": run["run_id"]}).get_json()
    second = client.post("/api/verifications/sync", json={"run_id": run["run_id"]}).get_json()
    assert {d["action"] for d in second["decisions"]} == {"noop"}
    assert first["metrics"]["parcels"] == second["metrics"]["parcels"]


def test_api_review_version_conflict():
    from app.main import create_app
    client = create_app().test_client()
    client.post("/api/runs", json={"sample": "clean.csv"})
    task = client.get("/api/verifications").get_json()["tasks"][0]
    url = f"/api/verifications/{task['task_id']}/review"
    assert client.post(url, json={"version": task["version"], "decision": "已核验"}).status_code == 200
    assert client.post(url, json={"version": task["version"], "decision": "已核验"}).status_code == 409
    assert client.post(url, json={"version": task["version"] + 1, "decision": "无效"}).status_code == 422


def test_verification_page_shows_owner_deadline_and_diff():
    from app.main import create_app
    client = create_app().test_client()
    client.post("/api/runs", json={"sample": "clean.csv"})
    page = client.get("/verifications").data.decode()
    assert "当前负责人" in page and "截止时间" in page and "原结论" in page and "新结论" in page
    assert "客服A" in page
