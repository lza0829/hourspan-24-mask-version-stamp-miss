"""掩码日历世代钉扎（mask_version_stamp，口径甲：段上钉死位图快照为准）。

夹具主链：改 mask 开段后核位图钉死 → 站规热更后旧钉不漂 → 只改落存位图，
两架构 valid 可区分（甲判 pinned_bitmap_mismatch；乙按现行站规重算则 valid 不动）。
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

# app.db 在首次导入时定死 DB_PATH；复用基线测试模块的同一数据目录，
# 保证两个测试文件的清库夹具作用于同一个真实库文件，互不污染。
import test_weekday_mask_split as baseline  # noqa: E402

_TMP = baseline._TMP
os.environ["DATA_DIR"] = _TMP
os.environ["FIXTURES_DIR"] = str(ROOT / "fixtures")

from app import seed  # noqa: E402
from app.db import connect  # noqa: E402
from app.modules import mask_version_stamp as mvs  # noqa: E402
from app.modules import weekday_mask_split as wms  # noqa: E402
from app.services import ingest, scd  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    db = Path(_TMP) / "hourspan.db"
    if db.exists():
        db.unlink()
    seed.init_db()
    yield


def _db():
    return connect()


def _apply_mask_fixture():
    batch = ingest.run_batch("mask_only_flip.json")
    run = wms.run_detect(batch["batch_id"])
    result = scd.apply_type2(run["detect_run_id"])
    return batch, run, result


def _pins(conn):
    return {
        r["hist_id"]: dict(r)
        for r in conn.execute("SELECT * FROM mask_calendar_epoch_pins").fetchall()
    }


def _current_hist(conn, store_id):
    return conn.execute(
        "SELECT * FROM dim_store_hours_hist WHERE store_id=? AND is_current=1",
        (store_id,),
    ).fetchone()


def test_mask_open_pins_calendar_epoch_same_transaction():
    """夹具改 mask 开段后：新段必须钉死日历世代（epoch + 七日位图快照 + 钉扎时刻）。"""
    _, run, result = _apply_mask_fixture()
    assert result["ok"] is True, result

    conn = _db()
    try:
        cur = mvs.current_calendar(conn)
        assert cur["epoch"] == 1 and cur["bitmap"] == "1111100"
        pins = _pins(conn)
        assert len(pins) == 8  # 6 段种子钉 + 2 段新开钉
        for store_id, new_mask in (("ST-WEST-01", "1111110"), ("ST-NORTH-01", "0111110")):
            hist = _current_hist(conn, store_id)
            pin = pins[hist["id"]]
            assert pin["epoch"] == cur["epoch"]  # 新段 epoch 与开段时站规一致，不分叉
            assert pin["bitmap"] == new_mask  # 七日位图快照钉死为新段 mask
            assert pin["bitmap"] == hist["weekday_mask"]
            assert pin["store_id"] == store_id
            assert pin["detect_run_id"] == run["detect_run_id"]
            assert pin["pinned_at"]
        by_store = {a["store_id"]: a for a in result["applied"]}
        assert by_store["ST-WEST-01"]["calendar_epoch"] == 1
        assert by_store["ST-NORTH-01"]["calendar_epoch"] == 1
        # 段级钉扎查询钩子不再空转
        stamp = wms.mask_calendar_epoch_for(conn, _current_hist(conn, "ST-WEST-01")["id"])
        assert stamp["stamped"] is True and stamp["epoch"] == 1
    finally:
        conn.close()

    # 只读核证：全部段钉扎有效
    conn = _db()
    try:
        att = mvs.attest(conn)
    finally:
        conn.close()
    assert att["checked"] == 8
    assert att["failed_count"] == 0, att
    assert att["architecture"] == "pinned_bitmap_authoritative"

    # mask 开段核证与日历世代协同：proof 行带世代钉扎且全 valid
    proof = wms.mask_open_proof(detect_run_id=run["detect_run_id"])
    assert proof["failed_count"] == 0, proof
    for r in proof["results"]:
        assert r["calendar_epoch"] == 1
        assert r["epoch_attest_valid"] is True
        assert r["pinned_bitmap"] == r["new_mask"]


def test_apply_rolls_back_when_calendar_epoch_unpinnable():
    """半截失败整笔回滚：站规世代指针坏掉时，开段不得只成一半。"""
    batch = ingest.run_batch("mask_only_flip.json")
    run = wms.run_detect(batch["batch_id"])

    conn = _db()
    try:
        before_hist = conn.execute(
            "SELECT COUNT(*) AS c FROM dim_store_hours_hist"
        ).fetchone()["c"]
        before_pins = conn.execute(
            "SELECT COUNT(*) AS c FROM mask_calendar_epoch_pins"
        ).fetchone()["c"]
        # 破坏站规：世代指针指向台账缺行的世代（开段时钉不出日历世代）
        conn.execute("UPDATE settings SET value='99' WHERE key='mask_calendar_epoch'")
        conn.commit()
    finally:
        conn.close()

    result = scd.apply_type2(run["detect_run_id"])
    assert result["ok"] is False
    assert result["code"] == "station_calendar_missing"

    conn = _db()
    try:
        # 整笔回滚：历史维、钉扎、写回标记、当前镜全部原样
        assert (
            conn.execute("SELECT COUNT(*) AS c FROM dim_store_hours_hist").fetchone()["c"]
            == before_hist
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) AS c FROM mask_calendar_epoch_pins"
            ).fetchone()["c"]
            == before_pins
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) AS c FROM detect_apply_marks WHERE detect_run_id=?",
                (run["detect_run_id"],),
            ).fetchone()["c"]
            == 0
        )
        for store_id in ("ST-WEST-01", "ST-NORTH-01"):
            mirror = conn.execute(
                "SELECT * FROM dim_store_hours_current WHERE store_id=?", (store_id,)
            ).fetchone()
            assert mirror["weekday_mask"] == "1111100"
    finally:
        conn.close()


def test_hot_update_does_not_rewrite_old_pins_and_new_open_uses_new_epoch():
    """站规热更：旧钉扎一字不漂；热更后再开段钉的是开段时的现行世代。"""
    _, run, result = _apply_mask_fixture()
    assert result["ok"] is True

    conn = _db()
    try:
        pins_before = _pins(conn)
    finally:
        conn.close()

    hot = mvs.hot_update("1111111", "全周营业新标准")
    assert hot["ok"] is True and hot["epoch"] == 2
    assert hot["pins_touched"] == 0

    conn = _db()
    try:
        assert _pins(conn) == pins_before  # 旧钉不漂（禁止热更回溯改写）
        cur = mvs.current_calendar(conn)
        assert cur["epoch"] == 2 and cur["bitmap"] == "1111111"
        # 口径甲：旧段核证仍有效——只读段上钉死位图，不按现行站规重算
        att = mvs.attest(conn)
        assert att["failed_count"] == 0, att
        west = [
            r
            for r in att["results"]
            if r["store_id"] == "ST-WEST-01" and r["is_current"] == 1
        ][0]
        assert west["epoch"] == 1
        assert west["epoch_is_current"] is False  # 仅审计展示，不算失败
        assert west["valid"] is True
    finally:
        conn.close()

    # 热更后再开段：新段钉的必须是开段时的现行站规世代（epoch=2）
    conn = _db()
    try:
        conn.execute(
            """INSERT INTO source_snapshots(
              id, batch_id, store_id, as_of_ts, open_time, close_time,
              weekday_mask, biz_status, payload_json, row_hash
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                "SNAP-MASK2-EAST-01",
                "B-MANUAL-2",
                "ST-EAST-01",
                "2026-11-20T00:00:00Z",
                "09:00",
                "21:00",
                "1101100",
                "open",
                "{}",
                seed.row_hash("09:00", "21:00", "1101100", "open"),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    run2 = wms.run_detect(None)
    east = [c for c in run2["changes"] if c["store_id"] == "ST-EAST-01"]
    assert len(east) == 1 and east[0]["kind"] == "mask_change"
    res2 = scd.apply_type2(run2["detect_run_id"])
    assert res2["ok"] is True, res2

    conn = _db()
    try:
        pins_now = _pins(conn)
        hist = _current_hist(conn, "ST-EAST-01")
        pin = pins_now[hist["id"]]
        assert pin["epoch"] == 2  # 与开段时站规世代一致，不分叉
        assert pin["bitmap"] == "1101100"
        # 邻键钉扎不被串改：热更 + 再开段后，既有钉扎仍逐字节原样
        west_hist = _current_hist(conn, "ST-WEST-01")
        assert pins_now[west_hist["id"]] == pins_before[west_hist["id"]]
        assert pins_now["HIST-SEED-ST-SOUTH-01"] == pins_before["HIST-SEED-ST-SOUTH-01"]
    finally:
        conn.close()


def test_tampered_stored_bitmap_flips_valid_architecture_distinguishable():
    """只改落存位图：两架构 valid 可区分。

    口径甲（本实现，钉死位图为准）：篡改钉扎位图 → pinned_bitmap_mismatch，valid 翻 False。
    口径乙（未采用，按现行站规重算、epoch 仅审计）：此次篡改不动 valid。
    故本断言即「两架构现场结论可区分」的判别器。
    """
    _, run, result = _apply_mask_fixture()
    assert result["ok"] is True

    conn = _db()
    try:
        west_hist = _current_hist(conn, "ST-WEST-01")
        north_hist = _current_hist(conn, "ST-NORTH-01")
        # 篡改落存的钉扎位图快照（不动段、不动站规台账）
        conn.execute(
            "UPDATE mask_calendar_epoch_pins SET bitmap='0000000' WHERE hist_id=?",
            (west_hist["id"],),
        )
        conn.commit()

        att = mvs.attest(conn, store_id="ST-WEST-01")
        west = [r for r in att["results"] if r["hist_id"] == west_hist["id"]][0]
        assert west["valid"] is False
        assert "pinned_bitmap_mismatch" in west["reasons"]

        # 邻键钉扎不被串改：NORTH 钉扎仍有效
        north = [
            r
            for r in mvs.attest(conn, store_id="ST-NORTH-01")["results"]
            if r["hist_id"] == north_hist["id"]
        ][0]
        assert north["valid"] is True
    finally:
        conn.close()

    # 核证页协同：同一篡改在 mask 开段核证同样判失败
    proof = wms.mask_open_proof(store_id="ST-WEST-01", detect_run_id=run["detect_run_id"])
    r = proof["results"][0]
    assert r["valid"] is False
    assert r["epoch_attest_valid"] is False
    assert "pinned_bitmap_mismatch" in r["reasons"]
    assert "pinned_bitmap_mismatch" in r["epoch_reasons"]


def test_apply_does_not_touch_adjacent_pins():
    """禁止邻键钉扎被串改：开新段只新增本段钉扎，存量钉扎逐字节不动。"""
    conn = _db()
    try:
        seed_pins = _pins(conn)
    finally:
        conn.close()
    assert len(seed_pins) == 6  # 种子段全部有钉（禁止有段无钉）

    _, _, result = _apply_mask_fixture()
    assert result["ok"] is True

    conn = _db()
    try:
        pins = _pins(conn)
        assert len(pins) == 8
        for hist_id, pin in seed_pins.items():
            assert pins[hist_id] == pin
    finally:
        conn.close()


def test_segment_without_epoch_pin_fails_attestation():
    """禁止开段成功而日历世代缺行：缺钉的段在核证中必须显式失败。"""
    _, run, result = _apply_mask_fixture()
    assert result["ok"] is True

    conn = _db()
    try:
        hist = _current_hist(conn, "ST-WEST-01")
        conn.execute(
            "DELETE FROM mask_calendar_epoch_pins WHERE hist_id=?", (hist["id"],)
        )
        conn.commit()
        att = mvs.attest(conn, hist_id=hist["id"])
        r = att["results"][0]
        assert r["valid"] is False
        assert "epoch_pin_missing" in r["reasons"]
    finally:
        conn.close()

    proof = wms.mask_open_proof(store_id="ST-WEST-01", detect_run_id=run["detect_run_id"])
    assert proof["results"][0]["valid"] is False
    assert "epoch_pin_missing" in proof["results"][0]["reasons"]


def test_api_epoch_attestation_read_only_and_hot_update_endpoint():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        ingest_resp = client.post(
            "/api/ingest/run", json={"fixture": "mask_only_flip.json"}
        ).json()
        detect_resp = client.post(
            "/api/detect/run", json={"batch_id": ingest_resp["batch_id"]}
        ).json()
        apply_resp = client.post(
            "/api/scd/apply", json={"detect_run_id": detect_resp["detect_run_id"]}
        ).json()
        assert apply_resp["ok"] is True

        before = client.get("/api/proofs/mask-calendar-epoch").json()
        assert before["checked"] == 8
        assert before["failed_count"] == 0
        assert before["current_epoch"] == 1
        hist_before = client.get("/api/history").json()
        stamps_before = client.get("/api/mask/calendar-stamps").json()
        assert stamps_before["epoch_ready"] is True
        assert stamps_before["current"]["epoch"] == 1
        assert stamps_before["hist_n"] == 8

        again = client.get("/api/proofs/mask-calendar-epoch").json()
        assert again == before  # 只读：两次核证结果一致
        assert client.get("/api/history").json() == hist_before  # 只读：历史维不变

        # 站规热更端点：换七日位图 → 追加新世代；旧钉不漂
        hot = client.post(
            "/api/settings/mask-calendar",
            json={"bitmap": "1111111", "note": "全周营业"},
        ).json()
        assert hot["ok"] is True and hot["epoch"] == 2
        stamps_after = client.get("/api/mask/calendar-stamps").json()
        assert stamps_after["current"]["epoch"] == 2
        assert stamps_after["stamps"] == stamps_before["stamps"]  # 旧钉一字不漂
        att_after = client.get("/api/proofs/mask-calendar-epoch").json()
        assert att_after["failed_count"] == 0  # 口径甲：旧段核证不漂

        # 同位图热更拒绝；非法位图 400
        noop = client.post("/api/settings/mask-calendar", json={"bitmap": "1111111"}).json()
        assert noop["ok"] is False and noop["code"] == "calendar_bitmap_unchanged"
        bad = client.post("/api/settings/mask-calendar", json={"bitmap": "222"})
        assert bad.status_code == 400
