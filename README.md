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
