import json

from app.db import connect
from app.modules import weekday_mask_split
from app.seed import canonical_row, now_iso, row_hash


def apply_type2(detect_run_id: str):
    """Close/open Type2 rows for a detect run; single transaction via weekday_mask_split."""
    return weekday_mask_split.apply_run(detect_run_id)


def rebuild_proofs():
    """Attest: mirror hist_id must point to the single is_current hist with canonical hash/mask."""
    conn = connect()
    now = now_iso()
    try:
        mirrors = conn.execute("SELECT * FROM dim_store_hours_current ORDER BY store_id").fetchall()
        results = []
        for m in mirrors:
            hist = conn.execute(
                "SELECT * FROM dim_store_hours_hist WHERE id=?", (m["hist_id"],)
            ).fetchone()
            current_count = conn.execute(
                "SELECT COUNT(*) AS c FROM dim_store_hours_hist WHERE store_id=? AND is_current=1",
                (m["store_id"],),
            ).fetchone()["c"]
            valid = 1
            detail = {}
            if not hist:
                valid = 0
                detail["error"] = "mirror_hist_missing"
            elif hist["is_current"] != 1:
                valid = 0
                detail["error"] = "mirror_not_current_hist"
            elif current_count != 1:
                valid = 0
                detail["error"] = "double_current"
            elif hist["row_hash"] != m["row_hash"]:
                valid = 0
                detail["error"] = "hash_mismatch"
            elif hist["store_id"] != m["store_id"]:
                valid = 0
                detail["error"] = "store_mismatch"
            elif hist["weekday_mask"] != m["weekday_mask"]:
                valid = 0
                detail["error"] = "mask_mismatch"
            else:
                canonical = row_hash(
                    hist["open_time"],
                    hist["close_time"],
                    hist["weekday_mask"],
                    hist["biz_status"],
                )
                if hist["row_hash"] != canonical or m["row_hash"] != canonical:
                    valid = 0
                    detail["error"] = "hash_not_include_mask"
                    detail["canonical"] = canonical
                    detail["canonical_src"] = canonical_row(
                        hist["open_time"],
                        hist["close_time"],
                        hist["weekday_mask"],
                        hist["biz_status"],
                    )
                else:
                    detail["ok"] = True
            conn.execute(
                """INSERT INTO scd_proofs(
                  store_id, hist_id, mirror_hist_id, row_hash, valid, detail_json, created_at
                ) VALUES (?,?,?,?,?,?,?)""",
                (
                    m["store_id"],
                    hist["id"] if hist else "",
                    m["hist_id"],
                    m["row_hash"],
                    valid,
                    json.dumps(detail, ensure_ascii=False),
                    now,
                ),
            )
            results.append(
                {
                    "store_id": m["store_id"],
                    "valid": bool(valid),
                    "detail": detail,
                    "mask": m["weekday_mask"],
                }
            )
        conn.commit()
        return {
            "checked": len(results),
            "failed": sum(1 for r in results if not r["valid"]),
            "results": results,
        }
    finally:
        conn.close()

# 掩码日历世代钉扎已在开段事务内同成败落库：见 modules/mask_version_stamp。
