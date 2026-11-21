"""weekday_mask_split · 仅 mask 位图翻转也必须开新段

主链路形态（甲）：标准 Type2 新段。
  - 检测以「现行有效段」(dim_store_hours_hist.is_current=1) 为基准，逐字段比对，
    weekday_mask 任一 bit 变化都算属性变更，绝不因其它字段未变而跳过。
  - 落库只开标准 Type2 新段（关旧 + 开新），禁止原地 UPDATE 历史维 mask（库内触发器兜底）。
  - row_hash 规范串固定为 ``open_time|close_time|weekday_mask|biz_status``；任何漏算
    mask 的实现，其落库 hash 与规范 hash 不符，核证直接失败。

失败闭路径（甲）：开闭 + 当前镜 + 本检测成员写回同一事务。
  - 单连接 BEGIN IMMEDIATE；任一校验失败整体 ROLLBACK，不存在「检测已记 mask 变更但
    历史维未新开」的半成品。
  - detect_apply_marks 做跑次级幂等；uq_hist_current_per_store 唯一部分索引禁止双 is_current。

只读核证：mask_open_proof() 回出旧/新 mask、hist_id、三方 hash（检测 to_hash / 历史行 /
当前镜）与规范 hash，供核证页调用；本函数只读不写。
"""

import json
from uuid import uuid4

from app.db import connect
from app.seed import mask_changed_bits, now_iso, row_hash

ATTR_FIELDS = ("open_time", "close_time", "weekday_mask", "biz_status")
HASH_TEMPLATE = "{open_time}|{close_time}|{weekday_mask}|{biz_status}"


class ApplyError(Exception):
    """事务内可预期的失败；触发整体回滚。"""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        self.message = message or code
        super().__init__(self.message)


def _fields(row):
    if row is None:
        return None
    return {f: row[f] for f in ATTR_FIELDS}


def _hash_fields(values) -> str:
    return row_hash(
        values.get("open_time"),
        values.get("close_time"),
        values.get("weekday_mask"),
        values.get("biz_status"),
    )


def effective_hist(conn, store_id: str):
    """现行有效段：每店应恰有一条 is_current=1。"""
    return conn.execute(
        "SELECT * FROM dim_store_hours_hist WHERE store_id=? AND is_current=1",
        (store_id,),
    ).fetchone()


def _latest_snapshots(conn, batch_id: str | None):
    """每店恰取一条最新快照（as_of_ts 最大；并列取 rowid 最大），避免重复成员。"""
    if batch_id:
        return conn.execute(
            """
            SELECT * FROM (
              SELECT s.*,
                     ROW_NUMBER() OVER (
                       PARTITION BY s.store_id ORDER BY s.as_of_ts DESC, s.rowid DESC
                     ) AS rn
              FROM source_snapshots s
              WHERE s.batch_id=?
            ) WHERE rn=1
            ORDER BY store_id
            """,
            (batch_id,),
        ).fetchall()
    return conn.execute(
        """
        SELECT * FROM (
          SELECT s.*,
                 ROW_NUMBER() OVER (
                   PARTITION BY s.store_id ORDER BY s.as_of_ts DESC, s.rowid DESC
                 ) AS rn
          FROM source_snapshots s
        ) WHERE rn=1
        ORDER BY store_id
        """
    ).fetchall()


def _safe_rollback(conn):
    try:
        conn.execute("ROLLBACK")
    except Exception:  # noqa: BLE001 - 事务可能尚未开启
        pass


def classify_change(conn, snap):
    """以现行有效段为基准判定变更；仅 mask 翻转也会得到 kind=mask_change。"""
    store_id = snap["store_id"]
    to = _fields(snap)
    to_hash = _hash_fields(to)
    cur = effective_hist(conn, store_id)
    mirror = conn.execute(
        "SELECT * FROM dim_store_hours_current WHERE store_id=?", (store_id,)
    ).fetchone()

    if cur is None:
        return {
            "store_id": store_id,
            "kind": "new",
            "snapshot_id": snap["id"],
            "from_hash": None,
            "to_hash": to_hash,
            "from": None,
            "to": to,
            "fields": sorted(ATTR_FIELDS),
            "mask_changed": True,
            "changed_bits": mask_changed_bits("", to["weekday_mask"]),
            "from_hist_id": None,
            "mirror_hash": mirror["row_hash"] if mirror else None,
            "mirror_mask": mirror["weekday_mask"] if mirror else None,
            "mirror_drift": bool(mirror),
        }

    frm = _fields(cur)
    field_diffs = [f for f in ATTR_FIELDS if frm[f] != to[f]]
    mask_changed = frm["weekday_mask"] != to["weekday_mask"]

    if field_diffs:
        return {
            "store_id": store_id,
            "kind": "mask_change" if mask_changed else "attr_change",
            "snapshot_id": snap["id"],
            "from_hash": cur["row_hash"],
            "to_hash": to_hash,
            "from": frm,
            "to": to,
            "fields": field_diffs,
            "mask_changed": mask_changed,
            "changed_bits": (
                mask_changed_bits(frm["weekday_mask"], to["weekday_mask"]) if mask_changed else ""
            ),
            "from_hist_id": cur["id"],
            "mirror_hash": mirror["row_hash"] if mirror else None,
            "mirror_mask": mirror["weekday_mask"] if mirror else None,
            "mirror_drift": bool(
                mirror
                and (
                    mirror["row_hash"] != cur["row_hash"]
                    or any(_fields(mirror)[f] != frm[f] for f in ATTR_FIELDS)
                )
            ),
        }

    # 与现行有效段无属性差异：只有当当前镜漂移时才需要修镜（不动历史维）。
    if mirror is None:
        return {
            "store_id": store_id,
            "kind": "mirror_drift",
            "snapshot_id": snap["id"],
            "from_hash": cur["row_hash"],
            "to_hash": to_hash,
            "from": frm,
            "to": to,
            "fields": [],
            "mask_changed": False,
            "changed_bits": "",
            "from_hist_id": cur["id"],
            "mirror_hash": None,
            "mirror_mask": None,
            "mirror_drift": True,
        }
    mirror_fields = _fields(mirror)
    if mirror["row_hash"] != cur["row_hash"] or any(
        mirror_fields[f] != frm[f] for f in ATTR_FIELDS
    ):
        return {
            "store_id": store_id,
            "kind": "mirror_drift",
            "snapshot_id": snap["id"],
            "from_hash": cur["row_hash"],
            "to_hash": to_hash,
            "from": frm,
            "to": to,
            "fields": [],
            "mask_changed": False,
            "changed_bits": "",
            "from_hist_id": cur["id"],
            "mirror_hash": mirror["row_hash"],
            "mirror_mask": mirror["weekday_mask"],
            "mirror_drift": True,
        }
    return None


def run_detect(batch_id: str | None = None):
    """对照现行有效段（不是当前镜）产出变更清单，mask 单独成因也照记。"""
    conn = connect()
    run_id = f"DET-{uuid4().hex[:8]}"
    now = now_iso()
    try:
        snaps = _latest_snapshots(conn, batch_id)
        changes = []
        for snap in snaps:
            ch = classify_change(conn, snap)
            if ch is not None:
                changes.append(ch)
        report = {
            "changes": changes,
            "scanned": len(snaps),
            "compare_basis": "effective_hist",
            "hash_includes_mask": True,
            "hash_template": HASH_TEMPLATE,
            "mask_change_count": sum(1 for c in changes if c["kind"] == "mask_change"),
        }
        conn.execute(
            """INSERT INTO detect_runs(id, batch_id, created_at, changed_count, report_json)
               VALUES (?,?,?,?,?)""",
            (run_id, batch_id, now, len(changes), json.dumps(report, ensure_ascii=False)),
        )
        conn.commit()
        return {
            "detect_run_id": run_id,
            "changed_count": len(changes),
            "changes": changes,
            "mask_change_count": report["mask_change_count"],
            "compare_basis": report["compare_basis"],
        }
    finally:
        conn.close()


def _close_hist(conn, hist_id: str, valid_to: str, run_id: str):
    conn.execute(
        """UPDATE dim_store_hours_hist
           SET valid_to=?, is_current=0, closed_by_run=?
           WHERE id=?""",
        (valid_to, run_id, hist_id),
    )


def _insert_hist(conn, store_id: str, values, snap, run_id: str) -> str:
    new_id = f"HIST-{uuid4().hex[:10]}"
    conn.execute(
        """INSERT INTO dim_store_hours_hist(
          id, store_id, open_time, close_time, weekday_mask, biz_status,
          valid_from, valid_to, is_current, source_snapshot_id, row_hash, closed_by_run
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            new_id,
            store_id,
            values["open_time"],
            values["close_time"],
            values["weekday_mask"],
            values["biz_status"],
            snap["as_of_ts"],
            None,
            1,
            snap["id"],
            _hash_fields(values),
            None,
        ),
    )
    return new_id


def _upsert_mirror(conn, store_id: str, values, hist_id: str, as_of_ts: str):
    conn.execute(
        """INSERT INTO dim_store_hours_current(
          store_id, open_time, close_time, weekday_mask, biz_status,
          hist_id, as_of_ts, row_hash
        ) VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(store_id) DO UPDATE SET
          open_time=excluded.open_time,
          close_time=excluded.close_time,
          weekday_mask=excluded.weekday_mask,
          biz_status=excluded.biz_status,
          hist_id=excluded.hist_id,
          as_of_ts=excluded.as_of_ts,
          row_hash=excluded.row_hash
        """,
        (
            store_id,
            values["open_time"],
            values["close_time"],
            values["weekday_mask"],
            values["biz_status"],
            hist_id,
            as_of_ts,
            _hash_fields(values),
        ),
    )


def _assert_snapshot_hash(snap, change):
    """落库前强校验规范 hash：任何漏算 mask 的写法都会在此被挡下。"""
    expected = _hash_fields(_fields(snap))
    if snap["row_hash"] != expected:
        raise ApplyError(
            "snapshot_hash_mismatch",
            f"快照 {snap['id']} row_hash 与规范串（含 weekday_mask）不符",
        )
    if change.get("to_hash") != expected:
        raise ApplyError(
            "detect_to_hash_mismatch",
            f"检测成员 {snap['store_id']} to_hash 与规范 hash 不符",
        )


def _check_single_current(conn, store_id: str):
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM dim_store_hours_hist WHERE store_id=? AND is_current=1",
        (store_id,),
    ).fetchone()
    if row["c"] > 1:
        raise ApplyError("double_current", f"门店 {store_id} 已存在多条 is_current=1")


def apply_run(detect_run_id: str):
    """开闭 + 当前镜 + 本检测成员写回，单事务原子提交；失败整体回滚。"""
    conn = connect()
    conn.isolation_level = None  # 显式控制 BEGIN/COMMIT，保证失败闭
    applied = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        mark = conn.execute(
            "SELECT * FROM detect_apply_marks WHERE detect_run_id=?", (detect_run_id,)
        ).fetchone()
        if mark:
            conn.execute("ROLLBACK")
            return {
                "ok": False,
                "code": "already_applied",
                "message": "该检测跑次已应用，拒绝重复开段",
                "detect_run_id": detect_run_id,
            }
        run = conn.execute(
            "SELECT * FROM detect_runs WHERE id=?", (detect_run_id,)
        ).fetchone()
        if not run:
            conn.execute("ROLLBACK")
            return {"ok": False, "code": "run_missing", "message": "检测跑次不存在"}

        report = json.loads(run["report_json"])
        changes = report.get("changes", [])
        for ch in changes:
            store_id = ch["store_id"]
            snap = conn.execute(
                "SELECT * FROM source_snapshots WHERE id=?", (ch["snapshot_id"],)
            ).fetchone()
            if not snap:
                raise ApplyError("snapshot_missing", f"快照 {ch['snapshot_id']} 缺失")
            if snap["store_id"] != store_id:
                raise ApplyError("snapshot_store_mismatch", "快照门店与检测成员不一致")
            _assert_snapshot_hash(snap, ch)
            _check_single_current(conn, store_id)
            cur = effective_hist(conn, store_id)
            to = _fields(snap)

            if ch["kind"] == "new":
                if cur is not None:
                    raise ApplyError(
                        "stale_new_effective_exists",
                        f"{store_id} 已有现行有效段，检测报告已过期",
                    )
                new_id = _insert_hist(conn, store_id, to, snap, detect_run_id)
                _upsert_mirror(conn, store_id, to, new_id, snap["as_of_ts"])
                applied.append(
                    {
                        "store_id": store_id,
                        "kind": "new",
                        "hist_id": new_id,
                        "closed_hist_id": None,
                        "old_mask": None,
                        "new_mask": to["weekday_mask"],
                        "changed_bits": ch.get("changed_bits", ""),
                        "detect_to_hash": ch["to_hash"],
                    }
                )
                continue

            if cur is None:
                raise ApplyError(
                    "stale_missing_effective", f"{store_id} 无现行有效段，检测报告已过期"
                )
            if cur["row_hash"] != ch.get("from_hash"):
                raise ApplyError(
                    "stale_from_hash",
                    f"{store_id} 现行有效段已变化，检测报告已过期",
                )
            if cur["id"] != ch.get("from_hist_id"):
                raise ApplyError(
                    "stale_from_hist", f"{store_id} 现行有效段 hist_id 与检测成员不一致"
                )

            if ch["kind"] == "mirror_drift":
                if any(_fields(cur)[f] != to[f] for f in ATTR_FIELDS):
                    raise ApplyError(
                        "stale_mirror_drift",
                        f"{store_id} 历史维已变化，不能仅修镜",
                    )
                _upsert_mirror(conn, store_id, to, cur["id"], snap["as_of_ts"])
                applied.append(
                    {
                        "store_id": store_id,
                        "kind": "mirror_drift",
                        "hist_id": cur["id"],
                        "closed_hist_id": None,
                        "old_mask": cur["weekday_mask"],
                        "new_mask": to["weekday_mask"],
                        "changed_bits": "",
                        "detect_to_hash": ch["to_hash"],
                    }
                )
                continue

            if snap["as_of_ts"] < cur["valid_from"]:
                raise ApplyError(
                    "as_of_before_valid_from",
                    f"{store_id} 快照 as_of 早于现行有效段 valid_from",
                )
            _close_hist(conn, cur["id"], snap["as_of_ts"], detect_run_id)
            new_id = _insert_hist(conn, store_id, to, snap, detect_run_id)
            _upsert_mirror(conn, store_id, to, new_id, snap["as_of_ts"])
            applied.append(
                {
                    "store_id": store_id,
                    "kind": ch["kind"],
                    "hist_id": new_id,
                    "closed_hist_id": cur["id"],
                    "old_mask": cur["weekday_mask"],
                    "new_mask": to["weekday_mask"],
                    "changed_bits": mask_changed_bits(
                        cur["weekday_mask"], to["weekday_mask"]
                    ),
                    "detect_to_hash": ch["to_hash"],
                }
            )

        result = {
            "ok": True,
            "detect_run_id": detect_run_id,
            "applied": applied,
            "count": len(applied),
            "mask_change_count": sum(1 for a in applied if a["kind"] == "mask_change"),
        }
        # 本检测成员写回：与开闭/镜同事务，提交后不可能只做一半。
        conn.execute(
            """INSERT INTO detect_apply_marks(detect_run_id, applied_at, result_json)
               VALUES (?,?,?)""",
            (detect_run_id, now_iso(), json.dumps(result, ensure_ascii=False)),
        )
        conn.execute("COMMIT")
        return result
    except ApplyError as exc:
        _safe_rollback(conn)
        return {
            "ok": False,
            "code": exc.code,
            "message": exc.message,
            "detect_run_id": detect_run_id,
        }
    except Exception as exc:  # noqa: BLE001 - 失败必须整体回滚并显式返回
        _safe_rollback(conn)
        return {
            "ok": False,
            "code": "apply_failed",
            "message": str(exc),
            "detect_run_id": detect_run_id,
        }
    finally:
        conn.close()


def _distinct(reasons):
    seen = set()
    out = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _verify_mask_change(conn, run, change):
    """单条 mask 开段核证；只读。"""
    store_id = change["store_id"]
    snap = conn.execute(
        "SELECT * FROM source_snapshots WHERE id=?", (change["snapshot_id"],)
    ).fetchone()
    old = conn.execute(
        """SELECT * FROM dim_store_hours_hist
           WHERE store_id=? AND closed_by_run=?
           ORDER BY valid_to DESC, rowid DESC LIMIT 1""",
        (store_id, run["id"]),
    ).fetchone()
    # 新段以「本检测成员开出的那一段」为准（按其快照定位），而非只看当前有效段；
    # 这样即使之后又有新段把它顶掉，本跑次核证仍如实回出当次的旧/新 mask。
    new = None
    if snap:
        new = conn.execute(
            """SELECT * FROM dim_store_hours_hist
               WHERE store_id=? AND source_snapshot_id=? AND valid_from=?
               ORDER BY rowid DESC LIMIT 1""",
            (store_id, snap["id"], snap["as_of_ts"]),
        ).fetchone()
    if new is None:
        new = effective_hist(conn, store_id)
    mirror = conn.execute(
        "SELECT * FROM dim_store_hours_current WHERE store_id=?", (store_id,)
    ).fetchone()
    current_count = conn.execute(
        "SELECT COUNT(*) AS c FROM dim_store_hours_hist WHERE store_id=? AND is_current=1",
        (store_id,),
    ).fetchone()["c"]

    reasons = []
    expected_hash = _hash_fields(_fields(snap)) if snap else None
    detect_to_hash = change.get("to_hash")
    hist_row_hash = new["row_hash"] if new else None
    mirror_row_hash = mirror["row_hash"] if mirror else None

    if current_count != 1:
        reasons.append("current_count_not_one")

    if not snap:
        reasons.append("snapshot_missing")
    else:
        if snap["row_hash"] != expected_hash:
            reasons.append("snapshot_hash_not_canonical")
        if detect_to_hash != expected_hash:
            reasons.append("detect_to_hash_not_canonical")
        to_mask = (change.get("to") or {}).get("weekday_mask")
        if to_mask is not None and snap["weekday_mask"] != to_mask:
            reasons.append("snapshot_mask_drift")

    old_mask = old["weekday_mask"] if old else (change.get("from") or {}).get("weekday_mask")
    new_mask = new["weekday_mask"] if new else (change.get("to") or {}).get("weekday_mask")
    changed_bits = mask_changed_bits(old_mask, new_mask)
    if not changed_bits:
        reasons.append("mask_not_changed")

    if not old:
        reasons.append("old_segment_not_closed")
    else:
        if old["is_current"]:
            reasons.append("old_segment_still_current")
        if old["store_id"] != store_id:
            reasons.append("old_segment_store_mismatch")

    if not new:
        reasons.append("new_segment_missing")
    else:
        if snap and new["source_snapshot_id"] != snap["id"]:
            reasons.append("new_segment_not_from_detect_member")
        if snap and new["weekday_mask"] != snap["weekday_mask"]:
            reasons.append("hist_mask_mismatch")
        if new["row_hash"] != expected_hash:
            reasons.append("hist_hash_not_canonical")
        if snap and new["valid_from"] != snap["as_of_ts"]:
            reasons.append("new_valid_from_mismatch")
        if new["valid_to"] is not None:
            reasons.append("new_segment_already_closed")
        if new["is_current"] != 1:
            reasons.append("new_segment_not_current")

    # 核心禁止态：检测已记 mask 变更，但历史维未因本跑次新开段。
    applied_by_run = bool(
        new and snap and new["source_snapshot_id"] == snap["id"] and new["is_current"] == 1
    )
    if not applied_by_run:
        reasons.append("detect_change_not_applied")

    if not mirror:
        reasons.append("mirror_missing")
    else:
        if not new or mirror["hist_id"] != new["id"]:
            reasons.append("mirror_hist_mismatch")
        if snap and mirror["weekday_mask"] != snap["weekday_mask"]:
            reasons.append("mirror_mask_mismatch")
        if mirror["row_hash"] != expected_hash:
            reasons.append("mirror_hash_not_canonical")

    three_way_consistent = bool(
        snap
        and new
        and mirror
        and snap["weekday_mask"] == new["weekday_mask"] == mirror["weekday_mask"]
        and expected_hash is not None
        and detect_to_hash == hist_row_hash == mirror_row_hash == expected_hash
    )
    if not three_way_consistent:
        reasons.append("three_way_not_equal")

    return {
        "store_id": store_id,
        "detect_run_id": run["id"],
        "snapshot_id": change["snapshot_id"],
        "hist_id": new["id"] if new else None,
        "old_hist_id": old["id"] if old else None,
        "old_mask": old_mask,
        "new_mask": new_mask,
        "changed_bits": changed_bits,
        "detect_to_hash": detect_to_hash,
        "hist_row_hash": hist_row_hash,
        "mirror_row_hash": mirror_row_hash,
        "expected_hash": expected_hash,
        "old_hash": old["row_hash"] if old else None,
        "mirror_hist_id": mirror["hist_id"] if mirror else None,
        "three_way_consistent": three_way_consistent,
        "hash_includes_mask": True,
        "hash_template": HASH_TEMPLATE,
        "valid": not reasons,
        "reasons": _distinct(reasons),
    }


def mask_open_proof(store_id: str | None = None, detect_run_id: str | None = None):
    """只读核证：旧/新 mask、hist_id、三方 hash。

    未指定跑次时，优先取最近一个含 mask 变更的检测跑次；若该跑次检测已记变更但历史维
    尚未开段，核证会显式失败（old_segment_not_closed / three_way_not_equal）。
    """
    conn = connect()
    try:
        if detect_run_id:
            run = conn.execute(
                "SELECT * FROM detect_runs WHERE id=?", (detect_run_id,)
            ).fetchone()
            runs = [run] if run else []
        else:
            rows = conn.execute(
                "SELECT * FROM detect_runs ORDER BY created_at DESC, rowid DESC LIMIT 50"
            ).fetchall()
            runs = []
            for r in rows:
                try:
                    rep = json.loads(r["report_json"])
                except (TypeError, ValueError):
                    continue
                if any(c.get("kind") == "mask_change" for c in rep.get("changes", [])):
                    runs = [r]
                    break
            if not runs and rows:
                runs = [rows[0]]

        results = []
        for run in runs:
            try:
                report = json.loads(run["report_json"])
            except (TypeError, ValueError):
                report = {}
            for change in report.get("changes", []):
                if change.get("kind") != "mask_change":
                    continue
                if store_id and change.get("store_id") != store_id:
                    continue
                results.append(_verify_mask_change(conn, run, change))

        return {
            "detect_run_id": runs[0]["id"] if runs else None,
            "store_id": store_id,
            "checked": len(results),
            "valid_count": sum(1 for r in results if r["valid"]),
            "failed_count": sum(1 for r in results if not r["valid"]),
            "results": results,
        }
    finally:
        conn.close()


def mask_calendar_epoch_for(conn, hist_id: str) -> dict:
    # 掩码开段主链可成；日历世代钉扎未落
    return {"hist_id": hist_id, "epoch": None, "bitmap": None, "stamped": False}
