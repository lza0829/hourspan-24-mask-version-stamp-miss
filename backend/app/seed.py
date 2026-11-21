import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from app.db import FIXTURES_DIR, connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stores (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  region TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_batches (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  report_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS source_snapshots (
  id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL,
  store_id TEXT NOT NULL REFERENCES stores(id),
  as_of_ts TEXT NOT NULL,
  open_time TEXT,
  close_time TEXT,
  weekday_mask TEXT NOT NULL DEFAULT '1111100',
  biz_status TEXT NOT NULL DEFAULT 'open',
  payload_json TEXT NOT NULL DEFAULT '{}',
  row_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS detect_runs (
  id TEXT PRIMARY KEY,
  batch_id TEXT,
  created_at TEXT NOT NULL,
  changed_count INTEGER NOT NULL DEFAULT 0,
  report_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS dim_store_hours_hist (
  id TEXT PRIMARY KEY,
  store_id TEXT NOT NULL REFERENCES stores(id),
  open_time TEXT,
  close_time TEXT,
  weekday_mask TEXT NOT NULL,
  biz_status TEXT NOT NULL,
  valid_from TEXT NOT NULL,
  valid_to TEXT,
  is_current INTEGER NOT NULL DEFAULT 1,
  source_snapshot_id TEXT,
  row_hash TEXT NOT NULL,
  closed_by_run TEXT
);

CREATE TABLE IF NOT EXISTS dim_store_hours_current (
  store_id TEXT PRIMARY KEY REFERENCES stores(id),
  open_time TEXT,
  close_time TEXT,
  weekday_mask TEXT NOT NULL,
  biz_status TEXT NOT NULL,
  hist_id TEXT NOT NULL,
  as_of_ts TEXT NOT NULL,
  row_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quarantine (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reason TEXT NOT NULL,
  member_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scd_proofs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  store_id TEXT NOT NULL,
  hist_id TEXT NOT NULL,
  mirror_hist_id TEXT NOT NULL,
  row_hash TEXT NOT NULL,
  valid INTEGER NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

-- 检测成员写回标记：与开闭/镜同事务写入，保证「检测已记变更」与「历史维已开段」
-- 不可能一半成功。再次应用同一跑次时据此幂等拒绝。
-- 落库形态取「甲」：标准 Type2 新段（不另立 mask 变更边表，避免形态二义）。
CREATE TABLE IF NOT EXISTS detect_apply_marks (
  detect_run_id TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL,
  result_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_hist_store_current
  ON dim_store_hours_hist(store_id, is_current);
CREATE INDEX IF NOT EXISTS idx_snap_store_ts
  ON source_snapshots(store_id, as_of_ts);

-- 禁止原地改历史维 mask：mask 变化只能关旧开新。
CREATE TRIGGER IF NOT EXISTS trg_hist_mask_no_inplace_update
BEFORE UPDATE OF weekday_mask ON dim_store_hours_hist
WHEN OLD.weekday_mask IS NOT NEW.weekday_mask
BEGIN
  SELECT RAISE(ABORT, 'hist_weekday_mask_in_place_update_forbidden');
END;
"""


def _ensure_current_unique_index(conn):
    """每个门店至多一条 is_current=1；已有脏数据时保留启动能力但显式跳过。"""
    try:
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS uq_hist_current_per_store
               ON dim_store_hours_hist(store_id) WHERE is_current=1"""
        )
    except sqlite3.IntegrityError:
        pass


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_row(open_time, close_time, weekday_mask, biz_status) -> str:
    """营业属性规范串。weekday_mask 是必含字段，任何漏算 mask 的变体都会得到不同 hash。"""
    return f"{open_time or ''}|{close_time or ''}|{weekday_mask or ''}|{biz_status or ''}"


def row_hash(open_time, close_time, weekday_mask, biz_status) -> str:
    return hashlib.sha256(canonical_row(open_time, close_time, weekday_mask, biz_status).encode("utf-8")).hexdigest()[:16]


def mask_bits(mask: str | None) -> str:
    return (mask or "").strip()


def mask_changed_bits(old_mask: str | None, new_mask: str | None) -> str:
    """返回发生翻转的位下标（从 0 起），长度不一致时按最长比较、缺位视为翻转。"""
    old = mask_bits(old_mask)
    new = mask_bits(new_mask)
    changed = []
    for i in range(max(len(old), len(new))):
        a = old[i] if i < len(old) else ""
        b = new[i] if i < len(new) else ""
        if a != b:
            changed.append(str(i))
    return ",".join(changed)


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    _ensure_current_unique_index(conn)
    if not conn.execute("SELECT 1 FROM settings WHERE key='as_of_mode'").fetchone():
        conn.execute("INSERT INTO settings(key, value) VALUES ('as_of_mode', 'business_ts')")
    if not conn.execute("SELECT 1 FROM settings WHERE key='mask_hash_version'").fetchone():
        conn.execute("INSERT INTO settings(key, value) VALUES ('mask_hash_version', 'mask-in-canonical-v1')")
    if conn.execute("SELECT COUNT(*) AS c FROM stores").fetchone()["c"] == 0:
        _seed(conn)
    conn.commit()
    conn.close()


def _seed(conn):
    path = FIXTURES_DIR / "seed_stores.json"
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    batch_id = "SEED-BATCH"
    created = now_iso()
    conn.execute(
        "INSERT INTO ingest_batches(id, created_at, report_json) VALUES (?,?,?)",
        (batch_id, created, json.dumps({"seed": True}, ensure_ascii=False)),
    )
    for s in data.get("stores", []):
        conn.execute(
            "INSERT INTO stores(id, name, region) VALUES (?,?,?)",
            (s["id"], s["name"], s["region"]),
        )
        ot, ct = s.get("open_time"), s.get("close_time")
        wm = s.get("weekday_mask", "1111100")
        st = s.get("biz_status", "open")
        rh = row_hash(ot, ct, wm, st)
        snap_id = f"SNAP-SEED-{s['id']}"
        as_of = s.get("as_of_ts", "2026-11-01T00:00:00Z")
        conn.execute(
            """INSERT INTO source_snapshots(
              id, batch_id, store_id, as_of_ts, open_time, close_time,
              weekday_mask, biz_status, payload_json, row_hash
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                snap_id,
                batch_id,
                s["id"],
                as_of,
                ot,
                ct,
                wm,
                st,
                json.dumps(s.get("payload", {}), ensure_ascii=False),
                rh,
            ),
        )
        hist_id = f"HIST-SEED-{s['id']}"
        conn.execute(
            """INSERT INTO dim_store_hours_hist(
              id, store_id, open_time, close_time, weekday_mask, biz_status,
              valid_from, valid_to, is_current, source_snapshot_id, row_hash, closed_by_run
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (hist_id, s["id"], ot, ct, wm, st, as_of, None, 1, snap_id, rh, None),
        )
        conn.execute(
            """INSERT INTO dim_store_hours_current(
              store_id, open_time, close_time, weekday_mask, biz_status,
              hist_id, as_of_ts, row_hash
            ) VALUES (?,?,?,?,?,?,?,?)""",
            (s["id"], ot, ct, wm, st, hist_id, as_of, rh),
        )
        conn.execute(
            """INSERT INTO scd_proofs(
              store_id, hist_id, mirror_hist_id, row_hash, valid, detail_json, created_at
            ) VALUES (?,?,?,?,?,?,?)""",
            (
                s["id"],
                hist_id,
                hist_id,
                rh,
                1,
                json.dumps({"seed": True}, ensure_ascii=False),
                created,
            ),
        )
