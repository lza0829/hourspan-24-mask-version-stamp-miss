import React, { useEffect, useState } from "react";
import { api } from "../api.js";

export default function Proofs() {
  const [rows, setRows] = useState([]);
  const [companion, setCompanion] = useState(null);
  const [epochAtt, setEpochAtt] = useState(null);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const reload = () => {
    api("/api/proofs").then(setRows).catch((e) => setErr(String(e.message || e)));
    Promise.resolve(api("/api/mask/calendar-stamps")).then(setCompanion).catch(() => setCompanion(null));
    Promise.resolve(api("/api/proofs/mask-calendar-epoch")).then(setEpochAtt).catch(() => setEpochAtt(null));
  };

  useEffect(() => { reload(); }, []);

  const rebuild = async () => {
    setErr("");
    try {
      const r = await api("/api/proofs/rebuild", { method: "POST", body: "{}" });
      setMsg(JSON.stringify(r, null, 2));
      await reload();
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  return (
    <>
      <div className="topbar">
        <div>
          <h1>核证</h1>
          <p>当前镜 hist_id / row_hash 必须与 is_current 历史行一致；失败须可观测。</p>
        </div>
      </div>
      <div className="panel">
        <div className="actions">
          <button onClick={rebuild}>重建核证</button>
          <button className="secondary" onClick={reload}>刷新</button>
        </div>
        {msg && <pre className="msg">{msg}</pre>}
        {err && <p className="error">{err}</p>}
      </div>
      
      <div className="panel">
        <h2>掩码日历世代钉扎</h2>
        <p className="muted">
          口径甲：以段上钉死位图快照为准——核验只读 hist 绑定七日位图，不按现行站规日历重算。
          {companion?.current && (
            <>
              {" "}现行站规世代 <span className="mono">epoch {companion.current.epoch}</span>{" "}
              位图 <span className="mono">{companion.current.bitmap}</span>
            </>
          )}
          {epochAtt && (
            <>
              {" "}· 核证 {epochAtt.checked} 段 / 失败{" "}
              <span className={epochAtt.failed_count ? "error" : "muted"}>{epochAtt.failed_count}</span>
            </>
          )}
        </p>
        <table>
          <thead>
            <tr>
              <th>门店</th><th>hist_id</th><th>段 mask</th><th>钉扎位图</th>
              <th>epoch</th><th>现行世代</th><th>钉扎时刻</th><th>valid</th><th>reasons</th>
            </tr>
          </thead>
          <tbody>
            {(epochAtt?.results || []).map((r) => (
              <tr key={r.hist_id}>
                <td>{r.store_id}</td>
                <td className="mono">{r.hist_id}</td>
                <td className="mono">{r.weekday_mask}</td>
                <td className="mono">{r.pinned_bitmap || "—"}</td>
                <td className="mono">{r.epoch ?? "—"}</td>
                <td>
                  {r.epoch == null ? "—" : (
                    <span className={`badge ${r.epoch_is_current ? "ok" : ""}`}>
                      {r.epoch_is_current ? "现行" : "历史世代"}
                    </span>
                  )}
                </td>
                <td className="mono">{r.pinned_at || "—"}</td>
                <td>
                  <span className={`badge ${r.valid ? "ok" : "bad"}`}>
                    {r.valid ? "通过" : "失败"}
                  </span>
                </td>
                <td className="mono">{(r.reasons || []).join(", ") || "—"}</td>
              </tr>
            ))}
            {(!epochAtt || (epochAtt.results || []).length === 0) && (
              <tr><td colSpan={9} className="muted">暂无历史段。</td></tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="panel">
        <h2>scd_proofs</h2>
        <table>
          <thead>
            <tr>
              <th>id</th><th>门店</th><th>hist</th><th>mirror_hist</th><th>hash</th><th>valid</th><th>detail</th><th>时间</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td>{r.id}</td>
                <td>{r.store_id}</td>
                <td className="mono">{r.hist_id}</td>
                <td className="mono">{r.mirror_hist_id}</td>
                <td className="mono">{r.row_hash}</td>
                <td>
                  <span className={`badge ${r.valid ? "ok" : "bad"}`}>
                    {r.valid ? "通过" : "失败"}
                  </span>
                </td>
                <td className="mono">{r.detail_json}</td>
                <td>{r.created_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
