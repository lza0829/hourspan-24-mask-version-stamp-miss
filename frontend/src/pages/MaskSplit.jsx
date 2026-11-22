import React, { useEffect, useState } from "react";
import { api } from "../api.js";

export default function MaskSplit() {
  const [rows, setRows] = useState([]);
  const [stores, setStores] = useState([]);
  const [storeId, setStoreId] = useState("");
  const [runId, setRunId] = useState("");
  const [batchId, setBatchId] = useState("");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const reload = () => {
    const q = new URLSearchParams();
    if (storeId.trim()) q.set("store_id", storeId.trim());
    if (runId.trim()) q.set("detect_run_id", runId.trim());
    const qs = q.toString();
    return api(`/api/proofs/mask-open${qs ? `?${qs}` : ""}`)
      .then((r) => { setRows(r.results || []); setMsg(JSON.stringify(r, null, 2)); })
      .catch((e) => setErr(String(e.message || e)));
  };

  useEffect(() => {
    api("/api/stores").then(setStores).catch(() => {});
    reload();
  }, []);

  const runFixture = async () => {
    setErr("");
    try {
      const b = await api("/api/ingest/run", {
        method: "POST",
        body: JSON.stringify({ fixture: "mask_only_flip.json" }),
      });
      setBatchId(b.batch_id || "");
      const d = await api("/api/detect/run", {
        method: "POST",
        body: JSON.stringify({ batch_id: b.batch_id }),
      });
      setRunId(d.detect_run_id || "");
      await reload();
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  const apply = async () => {
    if (!runId.trim()) return;
    setErr("");
    try {
      const r = await api("/api/scd/apply", {
        method: "POST",
        body: JSON.stringify({ detect_run_id: runId.trim() }),
      });
      setMsg(JSON.stringify(r, null, 2));
      await reload();
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  return (
    <>
      <div data-plant="24-mask-version-stamp-miss" className="plant-mark" style={{display:"none"}} />
      <div className="topbar">
        <div>
          <h1>mask 开段</h1>
          <p>
            weekday_mask 任一 bit 变化即属性变更：必须关旧开新。核证回出旧/新 mask、hist_id、
            检测 to_hash / 历史行 hash / 当前镜 hash 三方同值；开段同事务钉死掩码日历世代
            （epoch + 七日位图快照），站规热更不回溯改写旧钉。
          </p>
        </div>
      </div>
      <div className="panel">
        <div className="actions">
          <input
            value={storeId}
            onChange={(e) => setStoreId(e.target.value)}
            placeholder="门店（可选）"
            style={{ minWidth: 160, padding: "0.4rem 0.6rem", borderRadius: 8, border: "1px solid var(--line)", background: "#0c2226", color: "var(--text)" }}
          />
          <input
            value={runId}
            onChange={(e) => setRunId(e.target.value)}
            placeholder="detect_run_id（空=最近 mask 跑次）"
            style={{ minWidth: 240, padding: "0.4rem 0.6rem", borderRadius: 8, border: "1px solid var(--line)", background: "#0c2226", color: "var(--text)" }}
          />
          <button onClick={reload}>核证</button>
          <button className="secondary" onClick={runFixture}>入档 mask 夹具</button>
          <button onClick={apply}>应用本跑次开段</button>
          {batchId && <span className="muted mono">批 {batchId}</span>}
        </div>
        {msg && <pre className="msg">{msg}</pre>}
        {err && <p className="error">{err}</p>}
      </div>
      <div className="panel">
        <h2>mask 开段核证 detail</h2>
        <table>
          <thead>
            <tr>
              <th>门店</th><th>旧 mask</th><th>新 mask</th><th>翻转位</th>
              <th>old_hist</th><th>new hist_id</th>
              <th>detect to_hash</th><th>hist hash</th><th>mirror hash</th>
              <th>日历世代</th><th>钉扎位图</th>
              <th>三方一致</th><th>valid</th><th>reasons</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={`${r.store_id}-${i}`}>
                <td>{r.store_id}</td>
                <td className="mono">{r.old_mask}</td>
                <td className="mono">{r.new_mask}</td>
                <td className="mono">{r.changed_bits || "—"}</td>
                <td className="mono">{r.old_hist_id || "—"}</td>
                <td className="mono">{r.hist_id || "—"}</td>
                <td className="mono">{r.detect_to_hash}</td>
                <td className="mono">{r.hist_row_hash || "—"}</td>
                <td className="mono">{r.mirror_row_hash || "—"}</td>
                <td className="mono">
                  {r.calendar_epoch != null ? `epoch ${r.calendar_epoch}` : "—"}
                </td>
                <td className="mono">{r.pinned_bitmap || "—"}</td>
                <td>
                  <span className={`badge ${r.three_way_consistent ? "ok" : "bad"}`}>
                    {r.three_way_consistent ? "一致" : "不一致"}
                  </span>
                </td>
                <td>
                  <span className={`badge ${r.valid ? "ok" : "bad"}`}>
                    {r.valid ? "通过" : "失败"}
                  </span>
                </td>
                <td className="mono">{(r.reasons || []).join(", ") || "—"}</td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr><td colSpan={14} className="muted">暂无 mask 变更跑次；先入档 mask 夹具再核证。</td></tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="panel">
        <h2>本店现行有效段</h2>
        <table>
          <thead><tr><th>门店</th><th>mask</th><th>hist_id</th><th>hash</th></tr></thead>
          <tbody>
            {stores
              .filter((s) => !storeId.trim() || s.id === storeId.trim())
              .map((s) => (
                <tr key={s.id}>
                  <td>{s.id}</td>
                  <td className="mono">{s.weekday_mask}</td>
                  <td className="mono">{s.hist_id}</td>
                  <td className="mono">{s.row_hash}</td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
