from pathlib import Path
import os
import sqlite3
import sys

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
TEST_DATA_DIR = ROOT / "backend" / "data_test_freeze"
os.environ["FIXTURES_DIR"] = str(ROOT / "fixtures")

from app.main import app  # noqa: E402
from app.services import handover as svc  # noqa: E402


@pytest.fixture(scope="module")
def client():
    os.environ["DATA_DIR"] = str(TEST_DATA_DIR)
    db_path = TEST_DATA_DIR / "app.db"
    if db_path.exists():
        db_path.unlink()
    with TestClient(app) as c:
        yield c


def _batch(client, bid):
    rows = client.get("/api/batches").json()
    return next(b for b in rows if b["id"] == bid)


# 需求1：解冻后进入「待确认归还」，绝不直接变可交班在途
def test_unfreeze_goes_to_pending_not_in_transit(client):
    assert _batch(client, 6)["status"] == "冻结"
    # 非责任班次禁止操作
    r = client.post("/api/batches/6/unfreeze", json={"operator_shift_id": 2})
    assert r.status_code == 403
    r = client.post(
        "/api/batches/6/unfreeze", json={"operator_shift_id": 1, "reason": "排查完成"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "待确认归还"
    assert _batch(client, 6)["status"] == "待确认归还"


# 需求2：待确认归还禁止普通结清
def test_pending_forbids_normal_settle(client):
    for bid in (6, 7):
        r = client.post("/api/batches/%d/settle" % bid, json={"operator_shift_id": 1})
        assert r.status_code == 409
        assert "禁止普通结清" in r.json()["detail"]
        assert _batch(client, bid)["status"] == "待确认归还"


# 需求1+3：待确认归还/冻结不进交班清单，排除结果随快照固化
def test_pending_excluded_from_handover_snapshot(client):
    # 先把批次6再冻结，覆盖「冻结」与「待确认归还」两种排除原因
    r = client.post("/api/batches/6/refreeze", json={"operator_shift_id": 1})
    assert r.status_code == 200 and r.json()["status"] == "冻结"

    r = client.post("/api/handovers/start", json={"from_shift_id": 1, "to_shift_id": 2})
    assert r.status_code == 200, r.text
    hid = r.json()["id"]
    # 清单只含在途批次 1-4
    assert r.json()["batch_count"] == 4
    assert {i["biz_key"] for i in r.json()["items"]} == {
        "B20261115-01",
        "B20261115-02",
        "B20261115-03",
        "B20261114-09",
    }
    excluded = {x["biz_key"]: x for x in r.json()["excluded"]}
    assert set(excluded) == {"B20261115-04", "B20261115-05"}
    assert excluded["B20261115-04"]["status_at_snapshot"] == "冻结"
    assert excluded["B20261115-05"]["status_at_snapshot"] == "待确认归还"
    assert all(x["exclude_reason"] for x in excluded.values())
    # 固化：重新拉取详情，排除结果仍在
    again = client.get(f"/api/handovers/{hid}").json()
    assert {x["biz_key"] for x in again["excluded"]} == {"B20261115-04", "B20261115-05"}

    done = client.post(f"/api/handovers/{hid}/complete")
    assert done.json()["status"] == "已完成"
    # 在途批次整批过户，被排除的批次原地不动
    assert all(
        _batch(client, bid)["owner_shift_id"] == 2 for bid in (1, 2, 3, 4)
    )
    assert _batch(client, 6)["owner_shift_id"] == 1
    assert _batch(client, 7)["owner_shift_id"] == 1


# 需求1：确认归还必须由当前责任班次执行，确认后才可交班
def test_confirm_return_requires_owner_shift(client):
    r = client.post("/api/batches/6/unfreeze", json={"operator_shift_id": 1})
    assert r.json()["status"] == "待确认归还"
    r = client.post("/api/batches/6/confirm-return", json={"operator_shift_id": 2})
    assert r.status_code == 403
    assert _batch(client, 6)["status"] == "待确认归还"
    r = client.post("/api/batches/6/confirm-return", json={"operator_shift_id": 1})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "在途"
    # 审计链完整：解冻、确认归还均留痕
    actions = [e["action"] for e in client.get("/api/batches/6/events").json()]
    assert actions == ["冻结", "解冻", "再冻结", "解冻", "确认归还"]


# 需求2：待确认归还可报废、可再冻结，且留审计
def test_pending_scrap_and_refreeze_with_audit(client):
    r = client.post("/api/batches/7/refreeze", json={"operator_shift_id": 1})
    assert r.json()["status"] == "冻结"
    r = client.post("/api/batches/7/unfreeze", json={"operator_shift_id": 1})
    assert r.json()["status"] == "待确认归还"
    r = client.post(
        "/api/batches/7/scrap", json={"operator_shift_id": 1, "reason": "批次变质"}
    )
    assert r.json()["status"] == "已报废"
    # 终态禁止再操作
    r = client.post("/api/batches/7/unfreeze", json={"operator_shift_id": 1})
    assert r.status_code == 409
    events = client.get("/api/batches/7/events").json()
    assert [e["action"] for e in events] == ["冻结", "解冻", "再冻结", "解冻", "报废"]
    assert all(e["to_status"] for e in events)
    assert events[-1]["reason"] == "批次变质"


# 需求3：交接中批次禁止状态变更；完成时整单全有或全无，禁止跳过脏键
def test_complete_is_all_or_nothing(client):
    # 班次1此时只有批次6在途（1-4已过户给班次2，7已报废）
    start = client.post("/api/handovers/start", json={"from_shift_id": 1, "to_shift_id": 3})
    assert start.status_code == 200, start.text
    hid = start.json()["id"]
    assert [i["biz_key"] for i in start.json()["items"]] == ["B20261115-04"]
    # 交接中禁止冻结
    r = client.post("/api/batches/6/freeze", json={"operator_shift_id": 1})
    assert r.status_code == 409
    # 模拟清单键被外部力量改脏：完成时不得跳过，必须整单失败、不过户
    conn = sqlite3.connect(TEST_DATA_DIR / "app.db")
    conn.execute("UPDATE batches SET status = '待确认归还' WHERE id = 6")
    conn.commit()
    conn.close()
    done = client.post(f"/api/handovers/{hid}/complete")
    assert done.json()["status"] == "失败"
    assert "整单拒绝" in done.json()["reason"]
    b6 = _batch(client, 6)
    assert b6["owner_shift_id"] == 1  # 未过户
    reports = client.get("/api/reconcile").json()
    assert reports[0]["一致"] is False
    assert reports[0]["detail"]["不可过户键"] == ["B20261115-04"]


# 需求3：发起快照误含待确认键时整单拒绝（守卫单测）
def test_snapshot_guard_rejects_tainted_keys():
    tainted = [
        {"batch_id": 1, "biz_key": "B1", "status": "在途", "included": 1, "exclude_reason": ""},
        {"batch_id": 2, "biz_key": "B2", "status": "待确认归还", "included": 1, "exclude_reason": ""},
    ]
    with pytest.raises(HTTPException) as ei:
        svc._assert_snapshot_clean(tainted)
    assert ei.value.status_code == 409
    clean = [tainted[0]] + [
        {"batch_id": 2, "biz_key": "B2", "status": "待确认归还", "included": 0, "exclude_reason": "解冻后待确认归还，确认前禁止交班"}
    ]
    svc._assert_snapshot_clean(clean)  # 不抛异常


# 发起方没有在途批次时禁止开单（冻结/待确认归还不凑数）
def test_start_without_in_transit_rejected(client):
    r = client.post("/api/handovers/start", json={"from_shift_id": 3, "to_shift_id": 1})
    assert r.status_code == 400
    assert "在途" in r.json()["detail"]


# 需求4：三态在列表接口可区分
def test_three_states_distinguishable_in_list(client):
    rows = client.get("/api/batches").json()
    statuses = {b["biz_key"]: b["status"] for b in rows}
    assert statuses["B20261115-01"] == "在途"
    assert statuses["B20261115-04"] == "待确认归还"  # 上一用例改脏后保持待确认
    assert statuses["B20261115-05"] == "已报废"
