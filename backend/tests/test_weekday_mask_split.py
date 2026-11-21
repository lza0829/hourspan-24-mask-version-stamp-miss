import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

_TMP = tempfile.mkdtemp(prefix="hourspan-test-")
os.environ["DATA_DIR"] = _TMP
os.environ["FIXTURES_DIR"] = str(ROOT / "fixtures")

from app import seed  # noqa: E402
from app.db import connect  # noqa: E402
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


def _run_mask_fixture():
    batch = ingest.run_batch("mask_only_flip.json")
    run = wms.run_detect(batch["batch_id"])
    return batch, run


def test_mask_only_flip_detects_change():
    _, run = _run_mask_fixture()
    by_store = {c["store_id"]: c for c in run["changes"]}
    assert set(by_store) == {"ST-WEST-01", "ST-NORTH-01"}
    for store_id, expected_bits in (("ST-WEST-01", "5"), ("ST-NORTH-01", "0,5")):
        ch = by_store[store_id]
        assert ch["kind"] == "mask_change"
        assert ch["mask_changed"] is True
        assert ch["fields"] == ["weekday_mask"]
        assert ch["changed_bits"] == expected_bits
        assert ch["from"]["open_time"] == ch["to"]["open_time"]
        assert ch["from"]["close_time"] == ch["to"]["close_time"]
        assert ch["from"]["biz_status"] == ch["to"]["biz_status"]
        assert ch["from_hash"] != ch["to_hash"]


def test_mask_flip_opens_one_more_segment_and_three_way_consistent():
    _, run = _run_mask_fixture()
    result = scd.apply_type2(run["detect_run_id"])
    assert result["ok"] is True, result
    assert result["count"] == 2

    conn = _db()
    try:
        for store_id, old_mask, new_mask in (
            ("ST-WEST-01", "1111100", "1111110"),
            ("ST-NORTH-01", "1111100", "0111110"),
        ):
            rows = conn.execute(
                "SELECT * FROM dim_store_hours_hist WHERE store_id=? ORDER BY valid_from, id",
                (store_id,),
            ).fetchall()
            assert len(rows) == 2, f"{store_id} should have exactly one extra segment"
            current = [r for r in rows if r["is_current"] == 1]
            assert len(current) == 1
            assert current[0]["weekday_mask"] == new_mask
            closed = [r for r in rows if r["is_current"] == 0]
            assert len(closed) == 1
            assert closed[0]["weekday_mask"] == old_mask
            assert closed[0]["valid_to"] == current[0]["valid_from"]

            mirror = conn.execute(
                "SELECT * FROM dim_store_hours_current WHERE store_id=?", (store_id,)
            ).fetchone()
            assert mirror["hist_id"] == current[0]["id"]
            assert mirror["weekday_mask"] == new_mask
            assert mirror["row_hash"] == current[0]["row_hash"]
    finally:
        conn.close()

    proof = wms.mask_open_proof(detect_run_id=run["detect_run_id"])
    assert proof["checked"] == 2
    assert proof["failed_count"] == 0, proof
    for r in proof["results"]:
        assert r["valid"] is True
        assert r["three_way_consistent"] is True
        assert r["hash_includes_mask"] is True
        assert r["detect_to_hash"] == r["hist_row_hash"] == r["mirror_row_hash"]
        assert r["old_mask"] != r["new_mask"]
        assert r["old_hist_id"] and r["hist_id"] and r["old_hist_id"] != r["hist_id"]


def test_hash_without_mask_is_rejected_by_proof():
    _, run = _run_mask_fixture()
    assert scd.apply_type2(run["detect_run_id"])["ok"] is True

    # 模拟「row_hash 公式漏掉 mask」的实现：历史行/当前镜/快照都改写成不含 mask 的 hash。
    def legacy_hash(open_time, close_time, biz_status):
        import hashlib

        raw = f"{open_time or ''}|{close_time or ''}|{biz_status or ''}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    conn = _db()
    try:
        for store_id in ("ST-WEST-01", "ST-NORTH-01"):
            hist = conn.execute(
                "SELECT * FROM dim_store_hours_hist WHERE store_id=? AND is_current=1",
                (store_id,),
            ).fetchone()
            legacy = legacy_hash(hist["open_time"], hist["close_time"], hist["biz_status"])
            assert legacy != hist["row_hash"]
            # 只改 hash，不动 weekday_mask 列：原地改 mask 已被触发器禁止，这里模拟 hash 公式错。
            conn.execute(
                "UPDATE dim_store_hours_hist SET row_hash=? WHERE id=?",
                (legacy, hist["id"]),
            )
            conn.execute(
                "UPDATE dim_store_hours_current SET row_hash=? WHERE store_id=?",
                (legacy, store_id),
            )
            conn.execute(
                "UPDATE source_snapshots SET row_hash=? WHERE store_id=? AND as_of_ts=?",
                (legacy, store_id, hist["valid_from"]),
            )
        conn.commit()
    finally:
        conn.close()

    proof = wms.mask_open_proof(detect_run_id=run["detect_run_id"])
    assert proof["failed_count"] == proof["checked"] == 2, proof
    for r in proof["results"]:
        assert r["valid"] is False
        assert r["three_way_consistent"] is False
        assert (
            "snapshot_hash_not_canonical" in r["reasons"]
            or "hist_hash_not_canonical" in r["reasons"]
        )

    rebuilt = scd.rebuild_proofs()
    assert rebuilt["failed"] >= 2
    assert all(
        any(k in (r["detail"].get("error") or "") for k in ("hash_not_include_mask",))
        for r in rebuilt["results"]
        if r["store_id"] in {"ST-WEST-01", "ST-NORTH-01"}
    )


def test_in_place_mask_update_forbidden_by_trigger():
    conn = _db()
    try:
        hist = conn.execute(
            "SELECT * FROM dim_store_hours_hist WHERE store_id='ST-EAST-01' AND is_current=1"
        ).fetchone()
        with pytest.raises(Exception) as excinfo:
            conn.execute(
                "UPDATE dim_store_hours_hist SET weekday_mask='0000000' WHERE id=?",
                (hist["id"],),
            )
        assert "in_place" in str(excinfo.value)
    finally:
        conn.rollback()
        conn.close()


def test_double_apply_is_rejected_and_no_double_current():
    _, run = _run_mask_fixture()
    assert scd.apply_type2(run["detect_run_id"])["ok"] is True
    again = scd.apply_type2(run["detect_run_id"])
    assert again["ok"] is False
    assert again["code"] == "already_applied"

    conn = _db()
    try:
        for store_id in ("ST-WEST-01", "ST-NORTH-01"):
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM dim_store_hours_hist WHERE store_id=? AND is_current=1",
                (store_id,),
            ).fetchone()["c"]
            assert n == 1
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM dim_store_hours_hist WHERE store_id=?",
                (store_id,),
            ).fetchone()["c"]
            assert total == 2
    finally:
        conn.close()


def test_stale_run_fails_closed_without_partial_writes():
    _, run = _run_mask_fixture()
    conn = _db()
    try:
        before = conn.execute(
            "SELECT COUNT(*) AS c FROM dim_store_hours_hist"
        ).fetchone()["c"]
    finally:
        conn.close()

    # 篡改检测报告里的 from_hash，模拟报告已过期（现行有效段已在别处变化）。
    conn = _db()
    try:
        import json

        stored = conn.execute(
            "SELECT report_json FROM detect_runs WHERE id=?", (run["detect_run_id"],)
        ).fetchone()
        report = json.loads(stored["report_json"])
        report["changes"][0]["from_hash"] = "deadbeefdeadbeef"
        conn.execute(
            "UPDATE detect_runs SET report_json=? WHERE id=?",
            (json.dumps(report, ensure_ascii=False), run["detect_run_id"]),
        )
        conn.commit()
    finally:
        conn.close()

    result = scd.apply_type2(run["detect_run_id"])
    assert result["ok"] is False
    assert result["code"] in {"stale_from_hash", "detect_to_hash_mismatch"}

    conn = _db()
    try:
        after = conn.execute(
            "SELECT COUNT(*) AS c FROM dim_store_hours_hist"
        ).fetchone()["c"]
        assert after == before  # 整体回滚，无半成品
        marks = conn.execute(
            "SELECT COUNT(*) AS c FROM detect_apply_marks WHERE detect_run_id=?",
            (run["detect_run_id"],),
        ).fetchone()["c"]
        assert marks == 0
    finally:
        conn.close()


def test_unchanged_snapshot_does_not_open_segment():
    batch = ingest.run_batch("mask_only_flip.json")
    # 再次入档同一夹具（同 id 幂等跳过）后检测：无变化，不应开段。
    ingest.run_batch("mask_only_flip.json")
    run = wms.run_detect(batch["batch_id"])
    assert run["changed_count"] == 2  # 相对种子基线仍有 mask 差异
    scd.apply_type2(run["detect_run_id"])
    run2 = wms.run_detect(batch["batch_id"])
    assert run2["changed_count"] == 0, run2


def test_detect_uses_canonical_hash_and_does_not_skip_mask():
    """若实现按漏算 mask 的旧 hash 比快照，mask-only 变化会被当无变化；本实现必须仍报变更。"""
    legacy_west = seed.canonical_row  # 仅用于确认规范串包含 mask 位
    for mask in ("1111100", "1111110"):
        assert mask in legacy_west("08:30", "20:30", mask, "open")

    batch = ingest.run_batch("mask_only_flip.json")
    run = wms.run_detect(batch["batch_id"])
    assert run["changed_count"] == 2
    for ch in run["changes"]:
        assert ch["to"] != ch["from"]
        assert ch["to_hash"] == seed.row_hash(
            ch["to"]["open_time"],
            ch["to"]["close_time"],
            ch["to"]["weekday_mask"],
            ch["to"]["biz_status"],
        )
        assert ch["from_hash"] == seed.row_hash(
            ch["from"]["open_time"],
            ch["from"]["close_time"],
            ch["from"]["weekday_mask"],
            ch["from"]["biz_status"],
        )


def test_proof_catches_mask_changed_but_old_hist_not_closed():
    """禁止态：检测已记 mask 变更、镜也改了，但历史维未关旧开新。核证必须失败。"""
    _, run = _run_mask_fixture()
    assert scd.apply_type2(run["detect_run_id"])["ok"] is True

    conn = _db()
    try:
        hist = conn.execute(
            "SELECT * FROM dim_store_hours_hist WHERE store_id='ST-WEST-01' AND is_current=1"
        ).fetchone()
        # 把新段删掉，恢复旧段为 current（模拟「只改了镜、没开历史段」）。
        conn.execute("DELETE FROM dim_store_hours_hist WHERE id=?", (hist["id"],))
        old = conn.execute(
            "SELECT * FROM dim_store_hours_hist WHERE store_id='ST-WEST-01' ORDER BY rowid LIMIT 1"
        ).fetchone()
        conn.execute(
            "UPDATE dim_store_hours_hist SET is_current=1, valid_to=NULL WHERE id=?",
            (old["id"],),
        )
        conn.commit()
    finally:
        conn.close()

    proof = wms.mask_open_proof(
        store_id="ST-WEST-01", detect_run_id=run["detect_run_id"]
    )
    assert proof["checked"] == 1
    r = proof["results"][0]
    assert r["valid"] is False
    assert "detect_change_not_applied" in r["reasons"]
    assert r["three_way_consistent"] is False


def test_api_mask_open_proof_is_read_only():
    from fastapi.testclient import TestClient

    from app.main import app


    with TestClient(app) as client:
        ingest_resp = client.post("/api/ingest/run", json={"fixture": "mask_only_flip.json"}).json()
        detect_resp = client.post(
            "/api/detect/run", json={"batch_id": ingest_resp["batch_id"]}
        ).json()
        assert detect_resp["mask_change_count"] == 2

        proof_before = client.get(
            "/api/proofs/mask-open", params={"detect_run_id": detect_resp["detect_run_id"]}
        ).json()
        # 尚未应用开段：核证必须失败（不得假装通过）
        assert proof_before["checked"] == 2
        assert proof_before["failed_count"] == 2
        for r in proof_before["results"]:
            assert r["valid"] is False
            assert "detect_change_not_applied" in r["reasons"]

        apply_resp = client.post(
            "/api/scd/apply", json={"detect_run_id": detect_resp["detect_run_id"]}
        ).json()
        assert apply_resp["ok"] is True

        hist_before = client.get("/api/history").json()
        proof_after = client.get(
            "/api/proofs/mask-open", params={"detect_run_id": detect_resp["detect_run_id"]}
        ).json()
        hist_after = client.get("/api/history").json()
        # 只读：核证前后历史维条数与内容不变。
        assert hist_before == hist_after
        assert proof_after["failed_count"] == 0
        assert all(r["three_way_consistent"] for r in proof_after["results"])

