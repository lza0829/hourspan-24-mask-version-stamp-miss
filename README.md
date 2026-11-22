# 时档（HourSpan）

**骨架：** 门店营业属性贴源入档 → 业务键变更检测 → Type2 历史行开闭 → 当前镜投影 → 历史与当前互证

- 领域：数据工程（去皮=维表 SCD / 历史开闭 / 当前镜 / 核证；不是门店后台 CRUD）
- 端口：前端 **6200**，接口 **11200**（避开 Chrome 封锁的 6000–6063）
- 公有仓前缀：`lza0829/hourspan-NN-*`
- 栈：Python 3.12 + FastAPI + SQLite；前端 **React + Vite**
- 避让：勿撞筑层分层汇总、价舱改价调度、信汇多源去重、变流 CDC 游标、格湾占用态

本地冒烟：

```powershell
cd claude-workspace/hourspan/_scaffold/backend
pip install -r requirements.txt
$env:DATA_DIR="$PWD/data"; $env:FIXTURES_DIR="$PWD/../fixtures"
python -m uvicorn app.main:app --host 127.0.0.1 --port 11200

cd ../frontend
npm install
npm run dev
```

或：`docker compose up -d --build`（6200 / 11200）

## 已实现（本分支）：`weekday_mask_split`

仅 `weekday_mask` 位图翻转也算属性变更，必须关旧开新，禁止原地改 mask。

- 检测基准：现行有效段 `dim_store_hours_hist.is_current=1`，逐字段比对；mask 任一 bit 变化即 `kind=mask_change`，不会因其它字段未变而跳过。
- 落库形态（择一，取甲）：标准 Type2 新段（关旧 + 开新）；库内触发器禁止原地 UPDATE 历史维 mask。
- 失败闭（择一，取甲）：开闭 + 当前镜 + 本检测成员写回同一事务；失败整体回滚；`detect_apply_marks` 跑次级幂等；唯一部分索引禁止双 `is_current`。
- hash 规范串：`open_time|close_time|weekday_mask|biz_status`；漏算 mask 的 hash 会被核证判失败。
- 只读核证接口：`GET /api/proofs/mask-open?store_id=&detect_run_id=`，回出旧/新 mask、`old_hist_id`/`hist_id`、检测 `to_hash` / 历史行 hash / 当前镜 hash 三方比对与失败原因。
- 夹具：`fixtures/mask_only_flip.json`（只翻 mask，其它营业字段不变）。

核证页：前端「mask 开段」（`/mask-split`）可一键入档夹具 → 检测 → 开段 → 核证。

测试：`cd backend && python -m pytest tests -q`

## 已实现（本分支）：`mask_version_stamp`

掩码变化开段标成功时，同成败钉死「掩码日历世代」：开的是哪一版七日掩码表，段上可查；
站规掩码表热更不回溯改写旧段，旧段不被按新表重解释。

- 同事务钉扎：每次开新段（new / attr_change / mask_change）在同一事务写入
  `mask_calendar_epoch_pins`（`hist_id`、`epoch`、七日位图快照、钉扎时刻、detect_run_id）；
  钉扎失败整笔回滚——禁止开段成功而日历世代缺行。
- 世代不分叉：钉扎在事务内读现行站规世代（`settings.mask_calendar_epoch` →
  `mask_calendar_epochs` 台账）并当场钉死，新段 epoch 必与开段时站规一致。
- 站规热更：`POST /api/settings/mask-calendar`（`{bitmap, note}`）只追加新世代行 +
  前进指针，绝不 UPDATE 旧钉扎；同位图热更拒绝，非法位图 400。
- 架构口径（全局唯一，取甲）：**以段上钉死位图快照为准**——核验只读 hist 绑定七日位图，
  不按现行站规日历重算。对照口径（乙，未采用）：段上 epoch 仅审计、核验按现行表；
  两口径只在「只改落存位图」时分叉——甲判 `pinned_bitmap_mismatch`，乙不动 valid。
- 只读核证（新增）：`GET /api/proofs/mask-calendar-epoch?store_id=&hist_id=`，
  回出每段钉扎（epoch / 钉扎位图 / 钉扎时刻）、现行世代、valid 与失败原因
  （`epoch_pin_missing` / `pinned_bitmap_mismatch` / `epoch_unknown` / `pin_store_mismatch`）；
  `GET /api/mask/calendar-stamps` 回出钉扎台账与世代台账。
- 协同：mask 开段核证（`/api/proofs/mask-open`）每行附带 `calendar_epoch` /
  `pinned_bitmap` / `epoch_attest_valid`，钉扎缺失或被篡改同样判失败；
  核证页、mask 开段页、站规页均展示世代钉扎。
- 夹具链路：`mask_only_flip.json` 开段后核位图钉死 → 站规热更后旧钉不漂 →
  只改落存位图，两架构 valid 可区分（见 `tests/test_mask_calendar_epoch.py`）。

