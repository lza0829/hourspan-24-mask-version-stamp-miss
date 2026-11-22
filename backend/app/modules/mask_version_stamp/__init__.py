"""mask_version_stamp · 掩码日历世代钉扎（开段同成败）

问题（24-mask-version-stamp-miss）：仅 weekday_mask 变化已触发 Type2 开段、检测
hash 已刷新，但新段上看不到「掩码日历世代」钉扎——不知道开段时用的是哪一版
七日掩码表；随后改站规掩码表，旧段被按新表重解释。

修法：掩码变化开段标成功时必须同成败钉 mask_calendar_epoch
（hist_id、epoch、七日位图快照、钉扎时刻）；旧钉扎不得被站规热更改写；
半截失败整笔回滚。

架构口径（全局唯一，取甲）：以段上钉死位图快照为准。
  - 核验旧段 mask 只读 hist 绑定的七日位图快照（mask_calendar_epoch_pins），
    不按现行站规日历重算；站规热更只追加新世代，旧段核验结论不漂。
  - 对照口径（乙，未采用）：段上 epoch 仅审计、核验按现行站规表重算。
    两口径只在「只改落存位图」时现场结论分叉：乙不动 valid，甲判
    pinned_bitmap_mismatch——夹具末步据此区分两架构。

硬约束：
  - 禁止开段成功而日历世代缺行：钉扎与开段同一事务，缺一则整笔回滚。
  - 禁止新段 epoch 与开段时站规分叉：钉扎在事务内读现行站规世代并当场钉死。
  - 禁止热更回溯改旧段位图快照：hot_update 只追加世代行，绝不 UPDATE pins。
  - 禁止邻键钉扎被串改：钉扎按 hist_id 主键单行插入，不动其它行。
"""

import re
import sqlite3

from app.db import connect
from app.seed import now_iso

STATUS = "implemented"

# 全局唯一口径：段上钉死位图快照为准（甲）。
ARCHITECTURE = "pinned_bitmap_authoritative"
ARCHITECTURE_NOTE = "以段上钉死位图快照为准：核验只读 hist 绑定七日位图，不按现行站规日历重算"

BITMAP_RE = re.compile(r"^[01]{7}$")


class StampError(Exception):
    """钉扎链路可预期失败；开段事务内抛出即触发整笔回滚。"""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        self.message = message or code
        super().__init__(self.message)


def current_calendar(conn) -> dict:
    """现行站规掩码日历（settings 指针 → 世代台账行）。缺行/指针非法即失败闭。"""
    row = conn.execute("SELECT value FROM settings WHERE key='mask_calendar_epoch'").fetchone()
    if not row:
        raise StampError("station_calendar_missing", "站规掩码日历世代指针缺失")
    try:
        epoch = int(row["value"])
    except (TypeError, ValueError):
        raise StampError(
            "station_calendar_missing", f"站规掩码日历世代指针非法：{row['value']}"
        )
    cal = conn.execute(
        "SELECT * FROM mask_calendar_epochs WHERE epoch=?", (epoch,)
    ).fetchone()
    if not cal:
        raise StampError(
            "station_calendar_missing", f"站规掩码日历世代 {epoch} 在台账中缺行"
        )
    return {
        "epoch": epoch,
        "bitmap": cal["bitmap"],
        "note": cal["note"],
        "created_at": cal["created_at"],
    }


def pin_segment(
    conn,
    *,
    hist_id: str,
    store_id: str,
    bitmap: str,
    detect_run_id: str | None,
) -> dict:
    """在开段事务内钉日历世代：读现行站规世代并当场钉死（同成败）。

    调用方必须已持有写事务（BEGIN IMMEDIATE）：读世代与写钉扎在同一事务，
    新段 epoch 不可能与开段时站规分叉。任何失败抛 StampError → 整笔回滚。
    """
    cal = current_calendar(conn)
    pinned_at = now_iso()
    try:
        conn.execute(
            """INSERT INTO mask_calendar_epoch_pins(
              hist_id, store_id, epoch, bitmap, detect_run_id, pinned_at
            ) VALUES (?,?,?,?,?,?)""",
            (hist_id, store_id, cal["epoch"], bitmap, detect_run_id, pinned_at),
        )
    except sqlite3.IntegrityError as exc:
        raise StampError(
            "epoch_pin_conflict", f"hist {hist_id} 日历世代钉扎冲突（邻键不得串改）：{exc}"
        )
    return {
        "hist_id": hist_id,
        "store_id": store_id,
        "epoch": cal["epoch"],
        "bitmap": bitmap,
        "detect_run_id": detect_run_id,
        "pinned_at": pinned_at,
    }


def pin_of(conn, hist_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM mask_calendar_epoch_pins WHERE hist_id=?", (hist_id,)
    ).fetchone()
    return dict(row) if row else None


def check_segment_pin(conn, hist, pin: dict | None = None) -> list[str]:
    """架构甲核验：只读 hist 绑定的钉死位图快照，不按现行站规日历重算。

    注意：pin.epoch 落后于现行站规世代不算失败（热更合法推进站规，旧钉不漂）；
    只有钉扎缺失 / 钉扎位图与段位图不符 / 世代不可考 / 邻键错位才判失败。
    """
    if pin is None:
        pin = pin_of(conn, hist["id"])
    if pin is None:
        return ["epoch_pin_missing"]
    reasons = []
    if pin["store_id"] != hist["store_id"]:
        reasons.append("pin_store_mismatch")
    if pin["bitmap"] != hist["weekday_mask"]:
        reasons.append("pinned_bitmap_mismatch")
    known = conn.execute(
        "SELECT 1 AS x FROM mask_calendar_epochs WHERE epoch=?", (pin["epoch"],)
    ).fetchone()
    if not known:
        reasons.append("epoch_unknown")
    return reasons


def attest(conn, store_id: str | None = None, hist_id: str | None = None) -> dict:
    """只读核证：掩码日历世代钉扎。不写库。

    口径甲：valid 只看「段上钉死位图快照」与段本身是否一致、钉扎是否可考；
    epoch_is_current 仅审计展示（热更后旧段 epoch 落后现行属正常）。
    """
    q = "SELECT * FROM dim_store_hours_hist WHERE 1=1"
    args: list = []
    if store_id:
        q += " AND store_id=?"
        args.append(store_id)
    if hist_id:
        q += " AND id=?"
        args.append(hist_id)
    q += " ORDER BY store_id, valid_from, id"
    hists = conn.execute(q, args).fetchall()

    try:
        cur = current_calendar(conn)
        calendar_ok = True
    except StampError:
        cur = None
        calendar_ok = False

    results = []
    for h in hists:
        pin = pin_of(conn, h["id"])
        reasons = check_segment_pin(conn, h, pin)
        if not calendar_ok:
            reasons.append("station_calendar_missing")
        epoch_row = None
        if pin:
            epoch_row = conn.execute(
                "SELECT * FROM mask_calendar_epochs WHERE epoch=?", (pin["epoch"],)
            ).fetchone()
        results.append(
            {
                "hist_id": h["id"],
                "store_id": h["store_id"],
                "weekday_mask": h["weekday_mask"],
                "is_current": h["is_current"],
                "valid_from": h["valid_from"],
                "valid_to": h["valid_to"],
                "epoch": pin["epoch"] if pin else None,
                "pinned_bitmap": pin["bitmap"] if pin else None,
                "pinned_at": pin["pinned_at"] if pin else None,
                "detect_run_id": pin["detect_run_id"] if pin else None,
                "epoch_is_current": bool(pin and cur and pin["epoch"] == cur["epoch"]),
                "calendar_bitmap_at_epoch": epoch_row["bitmap"] if epoch_row else None,
                "valid": not reasons,
                "reasons": reasons,
            }
        )
    return {
        "architecture": ARCHITECTURE,
        "architecture_note": ARCHITECTURE_NOTE,
        "current_epoch": cur["epoch"] if cur else None,
        "current_bitmap": cur["bitmap"] if cur else None,
        "checked": len(results),
        "valid_count": sum(1 for r in results if r["valid"]),
        "failed_count": sum(1 for r in results if not r["valid"]),
        "results": results,
    }


def hot_update(bitmap: str, note: str = "") -> dict:
    """站规掩码日历热更：只追加新世代行 + 前进指针，绝不回溯改写旧钉扎。"""
    if not isinstance(bitmap, str) or not BITMAP_RE.match(bitmap):
        return {
            "ok": False,
            "code": "bitmap_format_invalid",
            "message": "bitmap 须为 7 位 0/1 位图",
        }
    conn = connect()
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = current_calendar(conn)
            if cur["bitmap"] == bitmap:
                raise StampError(
                    "calendar_bitmap_unchanged", "新位图与现行站规一致，无需热更"
                )
            new_epoch = cur["epoch"] + 1
            conn.execute(
                "INSERT INTO mask_calendar_epochs(epoch, bitmap, note, created_at)"
                " VALUES (?,?,?,?)",
                (new_epoch, bitmap, note or "", now_iso()),
            )
            conn.execute(
                "UPDATE settings SET value=? WHERE key='mask_calendar_epoch'",
                (str(new_epoch),),
            )
            # 硬约束：本事务绝不触碰 mask_calendar_epoch_pins（旧钉不漂、邻键不串）。
            conn.execute("COMMIT")
            return {
                "ok": True,
                "epoch": new_epoch,
                "bitmap": bitmap,
                "previous_epoch": cur["epoch"],
                "previous_bitmap": cur["bitmap"],
                "pins_touched": 0,
            }
        except StampError as exc:
            _rollback_quietly(conn)
            return {"ok": False, "code": exc.code, "message": exc.message}
        except Exception as exc:  # noqa: BLE001 - 失败必须整体回滚并显式返回
            _rollback_quietly(conn)
            return {"ok": False, "code": "hot_update_failed", "message": str(exc)}
    finally:
        conn.close()


def _rollback_quietly(conn):
    try:
        conn.execute("ROLLBACK")
    except Exception:  # noqa: BLE001 - 事务可能尚未开启
        pass


def stamps_of(conn, store_id: str | None = None) -> dict:
    """钉扎台账只读视图：现行站规日历 + 世代台账 + 各段钉扎。"""
    q = """
        SELECT p.hist_id, p.store_id, p.epoch, p.bitmap, p.detect_run_id, p.pinned_at,
               h.weekday_mask AS hist_mask, h.is_current, h.valid_from, h.valid_to
        FROM mask_calendar_epoch_pins p
        JOIN dim_store_hours_hist h ON h.id = p.hist_id
    """
    args: list = []
    if store_id:
        q += " WHERE p.store_id=?"
        args.append(store_id)
    q += " ORDER BY p.pinned_at DESC, p.hist_id LIMIT 100"
    stamps = [dict(r) for r in conn.execute(q, args).fetchall()]
    try:
        cur = current_calendar(conn)
    except StampError:
        cur = None
    epochs = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM mask_calendar_epochs ORDER BY epoch"
        ).fetchall()
    ]
    return {
        "epoch_ready": cur is not None,
        "architecture": ARCHITECTURE,
        "current": cur,
        "epochs": epochs,
        "hist_n": len(stamps),
        "stamps": stamps,
    }
