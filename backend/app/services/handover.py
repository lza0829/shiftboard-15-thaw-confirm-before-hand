from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import HTTPException

from app.db import connect, rows_to_dicts

# ---------------------------------------------------------------------------
# 批次状态机
#   在途 --冻结--> 冻结 --解冻--> 待确认归还 --确认归还--> 在途
#   待确认归还 --再冻结--> 冻结；待确认归还/冻结 --报废--> 已报废
#   在途 --结清--> 已结清
# 铁律：解冻只能落到「待确认归还」，绝不直接变在途；
#       只有当前责任班次执行「确认归还」后才回到可交班在途。
# ---------------------------------------------------------------------------
STATUS_IN_TRANSIT = "在途"
STATUS_FROZEN = "冻结"
STATUS_PENDING_RETURN = "待确认归还"
STATUS_SETTLED = "已结清"
STATUS_SCRAPPED = "已报废"

# action -> (允许的源状态, 目标状态, 审计动作名)
TRANSITIONS = {
    "freeze": ({STATUS_IN_TRANSIT}, STATUS_FROZEN, "冻结"),
    "unfreeze": ({STATUS_FROZEN}, STATUS_PENDING_RETURN, "解冻"),
    "confirm_return": ({STATUS_PENDING_RETURN}, STATUS_IN_TRANSIT, "确认归还"),
    "refreeze": ({STATUS_PENDING_RETURN}, STATUS_FROZEN, "再冻结"),
    "scrap": ({STATUS_FROZEN, STATUS_PENDING_RETURN}, STATUS_SCRAPPED, "报废"),
    "settle": ({STATUS_IN_TRANSIT}, STATUS_SETTLED, "结清"),
}

# 发起快照的排除原因：与批次状态同源，不在别处另造口径
EXCLUDE_REASONS = {
    STATUS_FROZEN: "批次冻结中，不参与交班",
    STATUS_PENDING_RETURN: "解冻后待确认归还，确认前禁止交班",
}

# 参与发起快照分类的在持状态（已结清/已报废为终态，不进入候选）
SNAPSHOT_CANDIDATE_STATUSES = (STATUS_IN_TRANSIT, STATUS_FROZEN, STATUS_PENDING_RETURN)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def list_shifts() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(conn.execute("SELECT * FROM shifts ORDER BY id").fetchall())


def list_batches(owner_shift_id: int | None = None) -> list[dict]:
    with connect() as conn:
        if owner_shift_id is None:
            rows = conn.execute(
                """
                SELECT b.*, s.name AS owner_shift_name
                FROM batches b
                LEFT JOIN shifts s ON s.id = b.owner_shift_id
                ORDER BY b.id
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT b.*, s.name AS owner_shift_name
                FROM batches b
                LEFT JOIN shifts s ON s.id = b.owner_shift_id
                WHERE b.owner_shift_id = ?
                ORDER BY b.id
                """,
                (owner_shift_id,),
            ).fetchall()
        return rows_to_dicts(rows)


def get_batch(batch_id: int) -> dict:
    with connect() as conn:
        b = conn.execute(
            """
            SELECT b.*, s.name AS owner_shift_name
            FROM batches b
            LEFT JOIN shifts s ON s.id = b.owner_shift_id
            WHERE b.id = ?
            """,
            (batch_id,),
        ).fetchone()
        if not b:
            raise HTTPException(404, "批次不存在")
        return dict(b)


def list_batch_events(batch_id: int) -> list[dict]:
    with connect() as conn:
        b = conn.execute("SELECT id FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if not b:
            raise HTTPException(404, "批次不存在")
        rows = conn.execute(
            """
            SELECT e.*, s.name AS operator_shift_name
            FROM batch_state_events e
            LEFT JOIN shifts s ON s.id = e.operator_shift_id
            WHERE e.batch_id = ?
            ORDER BY e.id
            """,
            (batch_id,),
        ).fetchall()
        return rows_to_dicts(rows)


def change_batch_status(
    batch_id: int, action: str, operator_shift_id: int, reason: str = ""
) -> dict:
    """批次状态机唯一入口：所有状态变迁都经此校验并落审计事件。"""
    if action not in TRANSITIONS:
        raise HTTPException(400, f"未知批次操作: {action}")
    allowed_from, to_status, action_label = TRANSITIONS[action]
    with connect() as conn:
        b = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if not b:
            raise HTTPException(404, "批次不存在")
        if b["status"] not in allowed_from:
            if b["status"] == STATUS_PENDING_RETURN and action == "settle":
                raise HTTPException(
                    409, "待确认归还批次禁止普通结清，须先确认归还、再冻结或报废"
                )
            raise HTTPException(
                409, f"批次当前状态「{b['status']}」不允许执行「{action_label}」"
            )
        # 责任节拍：状态变更（含确认归还）必须由当前责任班次执行
        if operator_shift_id != b["owner_shift_id"]:
            raise HTTPException(403, "必须由当前责任班次执行该批次的状态变更")
        active = conn.execute(
            """
            SELECT hi.handover_id AS hid
            FROM handover_items hi
            JOIN handovers h ON h.id = hi.handover_id
            WHERE hi.batch_id = ? AND h.status = '进行中'
            """,
            (batch_id,),
        ).fetchone()
        if active:
            raise HTTPException(
                409, f"批次已纳入进行中交班单#{active['hid']}，禁止状态变更"
            )
        conn.execute(
            "UPDATE batches SET status = ? WHERE id = ?", (to_status, batch_id)
        )
        conn.execute(
            """
            INSERT INTO batch_state_events(
              batch_id, biz_key, action, from_status, to_status,
              operator_shift_id, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                b["biz_key"],
                action_label,
                b["status"],
                to_status,
                operator_shift_id,
                reason,
                _now(),
            ),
        )
        conn.commit()
    return get_batch(batch_id)


def list_handovers() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT h.*,
                   fs.name AS from_shift_name,
                   ts.name AS to_shift_name
            FROM handovers h
            JOIN shifts fs ON fs.id = h.from_shift_id
            JOIN shifts ts ON ts.id = h.to_shift_id
            ORDER BY h.id DESC
            """
        ).fetchall()
        return rows_to_dicts(rows)


def get_handover(handover_id: int) -> dict:
    with connect() as conn:
        h = conn.execute(
            """
            SELECT h.*,
                   fs.name AS from_shift_name,
                   ts.name AS to_shift_name
            FROM handovers h
            JOIN shifts fs ON fs.id = h.from_shift_id
            JOIN shifts ts ON ts.id = h.to_shift_id
            WHERE h.id = ?
            """,
            (handover_id,),
        ).fetchone()
        if not h:
            raise HTTPException(404, "交班单不存在")
        items = conn.execute(
            """
            SELECT b.*
            FROM handover_items hi
            JOIN batches b ON b.id = hi.batch_id
            WHERE hi.handover_id = ?
            ORDER BY b.id
            """,
            (handover_id,),
        ).fetchall()
        # 发起快照中固化的排除结果：状态与排除原因同源
        excluded = conn.execute(
            """
            SELECT hs.batch_id, hs.biz_key, hs.status_at_snapshot, hs.exclude_reason,
                   b.title
            FROM handover_snapshot hs
            JOIN batches b ON b.id = hs.batch_id
            WHERE hs.handover_id = ? AND hs.included = 0
            ORDER BY hs.batch_id
            """,
            (handover_id,),
        ).fetchall()
        out = dict(h)
        out["items"] = rows_to_dicts(items)
        out["excluded"] = rows_to_dicts(excluded)
        return out


def dashboard() -> dict:
    with connect() as conn:
        shifts = rows_to_dicts(conn.execute("SELECT * FROM shifts ORDER BY id").fetchall())
        inflight = conn.execute(
            "SELECT COUNT(*) AS c FROM batches WHERE status = '在途'"
        ).fetchone()["c"]
        frozen = conn.execute(
            "SELECT COUNT(*) AS c FROM batches WHERE status = '冻结'"
        ).fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM batches WHERE status = '待确认归还'"
        ).fetchone()["c"]
        active = conn.execute(
            "SELECT COUNT(*) AS c FROM handovers WHERE status = '进行中'"
        ).fetchone()["c"]
        last = conn.execute(
            "SELECT * FROM handovers ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return {
            "班次数": len(shifts),
            "在途批次数": inflight,
            "冻结批次数": frozen,
            "待确认归还数": pending,
            "进行中交班": active,
            "最近交班": dict(last) if last else None,
            "班次": shifts,
        }


def _classify_snapshot_row(status: str) -> tuple[int, str]:
    """发起快照分类：只有在途批次纳入交班清单，其余记录排除原因（与状态同源）。"""
    if status == STATUS_IN_TRANSIT:
        return 1, ""
    return 0, EXCLUDE_REASONS.get(status, f"状态「{status}」不参与交班")


def _assert_snapshot_clean(snapshot_rows: list[dict]) -> None:
    """整单拒绝守卫：发起快照的纳入键必须全部是在途批次。

    若快照误含待确认归还/冻结等不可交班键，整单拒绝开单，
    禁止带着脏快照进入交班流程。
    """
    tainted = [r for r in snapshot_rows if r["included"] and r["status"] != STATUS_IN_TRANSIT]
    if tainted:
        keys = "、".join(r["biz_key"] for r in tainted)
        raise HTTPException(409, f"交班发起快照误含不可交班键（{keys}），整单拒绝")


def start_handover(from_shift_id: int, to_shift_id: int) -> dict:
    if from_shift_id == to_shift_id:
        raise HTTPException(400, "交班双方不能是同一班次")
    with connect() as conn:
        fs = conn.execute("SELECT * FROM shifts WHERE id = ?", (from_shift_id,)).fetchone()
        ts = conn.execute("SELECT * FROM shifts WHERE id = ?", (to_shift_id,)).fetchone()
        if not fs or not ts:
            raise HTTPException(404, "班次不存在")
        active = conn.execute(
            "SELECT id FROM handovers WHERE status = '进行中' LIMIT 1"
        ).fetchone()
        if active:
            raise HTTPException(409, f"已有进行中的交班单#{active['id']}，禁止并行交班")
        candidates = conn.execute(
            """
            SELECT id, biz_key, status FROM batches
            WHERE owner_shift_id = ? AND status IN (?, ?, ?)
            ORDER BY id
            """,
            (from_shift_id, *SNAPSHOT_CANDIDATE_STATUSES),
        ).fetchall()
        snapshot_rows = []
        for c in candidates:
            included, reason = _classify_snapshot_row(c["status"])
            snapshot_rows.append(
                {
                    "batch_id": c["id"],
                    "biz_key": c["biz_key"],
                    "status": c["status"],
                    "included": included,
                    "exclude_reason": reason,
                }
            )
        # 整单拒绝：快照纳入键必须全部在途，待确认归还/冻结绝不入清单
        _assert_snapshot_clean(snapshot_rows)
        included_rows = [r for r in snapshot_rows if r["included"]]
        if not included_rows:
            raise HTTPException(
                400, "交班方没有可交接的在途批次（冻结/待确认归还不计入交班清单）"
            )
        cur = conn.execute(
            """
            INSERT INTO handovers(from_shift_id, to_shift_id, status, batch_count, started_at)
            VALUES (?, ?, '进行中', ?, ?)
            """,
            (from_shift_id, to_shift_id, len(included_rows), _now()),
        )
        hid = cur.lastrowid
        for r in included_rows:
            conn.execute(
                "INSERT INTO handover_items(handover_id, batch_id) VALUES (?, ?)",
                (hid, r["batch_id"]),
            )
        # 发起排除结果固化：纳入/排除随快照落库，交班办理按此展示
        for r in snapshot_rows:
            conn.execute(
                """
                INSERT INTO handover_snapshot(
                  handover_id, batch_id, biz_key, status_at_snapshot, included, exclude_reason
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    hid,
                    r["batch_id"],
                    r["biz_key"],
                    r["status"],
                    r["included"],
                    r["exclude_reason"],
                ),
            )
        conn.execute("UPDATE shifts SET status = '交班中' WHERE id = ?", (from_shift_id,))
        conn.execute("UPDATE shifts SET status = '接班中' WHERE id = ?", (to_shift_id,))
        conn.commit()
    return get_handover(hid)


def complete_handover(handover_id: int) -> dict:
    with connect() as conn:
        h = conn.execute("SELECT * FROM handovers WHERE id = ?", (handover_id,)).fetchone()
        if not h:
            raise HTTPException(404, "交班单不存在")
        if h["status"] != "进行中":
            raise HTTPException(400, "只能完成进行中的交班单")
        items = conn.execute(
            """
            SELECT hi.batch_id, b.biz_key, b.status, b.owner_shift_id
            FROM handover_items hi
            JOIN batches b ON b.id = hi.batch_id
            WHERE hi.handover_id = ?
            ORDER BY hi.batch_id
            """,
            (handover_id,),
        ).fetchall()
        before = len(items)
        # 整单校验：任一清单键已不可过户（非在途或归属已变）即整单失败，
        # 禁止完成时跳过这些键造成清单与过户数不一致
        blocked = [
            it
            for it in items
            if it["status"] != STATUS_IN_TRANSIT
            or it["owner_shift_id"] != h["from_shift_id"]
        ]
        if not blocked:
            # 交班完成：整批过户，禁止只改交班单状态不改归属
            for it in items:
                conn.execute(
                    "UPDATE batches SET owner_shift_id = ? WHERE id = ? AND status = '在途'",
                    (h["to_shift_id"], it["batch_id"]),
                )
        after_from = conn.execute(
            "SELECT COUNT(*) AS c FROM batches WHERE owner_shift_id = ? AND status = '在途'",
            (h["from_shift_id"],),
        ).fetchone()["c"]
        after_to = conn.execute(
            "SELECT COUNT(*) AS c FROM batches WHERE owner_shift_id = ? AND status = '在途'",
            (h["to_shift_id"],),
        ).fetchone()["c"]
        consistent = 1 if not blocked and after_from == 0 and after_to >= before else 0
        if blocked:
            fail_reason = "清单含不可过户键（{}），整单拒绝完成".format(
                "、".join(it["biz_key"] for it in blocked)
            )
        else:
            fail_reason = "交班后批次归属与清单不一致"
        detail = {
            "交班前在途数": before,
            "交班后交班方在途": after_from,
            "交班后接班方在途": after_to,
            "不可过户键": [it["biz_key"] for it in blocked],
        }
        conn.execute(
            """
            UPDATE handovers
            SET status = ?, finished_at = ?, batch_count = ?
            WHERE id = ?
            """,
            ("已完成" if consistent else "失败", _now(), before, handover_id),
        )
        if consistent:
            conn.execute("UPDATE shifts SET status = '已交班' WHERE id = ?", (h["from_shift_id"],))
            conn.execute("UPDATE shifts SET status = '值班中' WHERE id = ?", (h["to_shift_id"],))
        else:
            conn.execute(
                "UPDATE handovers SET reason = ? WHERE id = ?",
                (fail_reason, handover_id),
            )
            conn.execute("UPDATE shifts SET status = '值班中' WHERE id = ?", (h["from_shift_id"],))
            conn.execute("UPDATE shifts SET status = '空闲' WHERE id = ?", (h["to_shift_id"],))
        conn.execute(
            """
            INSERT INTO reconcile_reports(
              handover_id, before_count, after_from_count, after_to_count,
              consistent, detail_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                handover_id,
                before,
                after_from,
                after_to,
                consistent,
                json.dumps(detail, ensure_ascii=False),
                _now(),
            ),
        )
        conn.commit()
    return get_handover(handover_id)


def list_reconcile() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM reconcile_reports ORDER BY id DESC"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json"))
            d["一致"] = bool(d.pop("consistent"))
            out.append(d)
        return out
