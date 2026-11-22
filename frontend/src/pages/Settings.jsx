import React, { useEffect, useState } from "react";
import { api } from "../api.js";

export default function Settings() {
  const [settings, setSettings] = useState({});
  const [stubs, setStubs] = useState([]);
  const [cal, setCal] = useState(null);
  const [bitmap, setBitmap] = useState("");
  const [note, setNote] = useState("");
  const [calMsg, setCalMsg] = useState("");
  const [err, setErr] = useState("");

  const loadCal = () =>
    api("/api/mask/calendar-stamps").then(setCal).catch(() => setCal(null));

  useEffect(() => {
    Promise.all([api("/api/settings"), api("/api/stubs")])
      .then(([s, st]) => { setSettings(s); setStubs(st.stubs || []); })
      .catch((e) => setErr(String(e.message || e)));
    loadCal();
  }, []);

  const hotUpdate = async () => {
    setErr("");
    setCalMsg("");
    try {
      const r = await api("/api/settings/mask-calendar", {
        method: "POST",
        body: JSON.stringify({ bitmap: bitmap.trim(), note: note.trim() }),
      });
      setCalMsg(JSON.stringify(r, null, 2));
      await loadCal();
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  return (
    <>
      <div className="topbar">
        <div>
          <h1>站规</h1>
          <p>端口 6200 / 11200 · 公有仓 lza0829/hourspan-* · 禁门店运营 CRUD 皮。</p>
        </div>
      </div>
      {err && <p className="error">{err}</p>}
      <div className="panel">
        <h2>设置</h2>
        <table>
          <thead><tr><th>键</th><th>值</th></tr></thead>
          <tbody>
            {Object.entries(settings).map(([k, v]) => (
              <tr key={k}><td>{k}</td><td className="mono">{v}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="panel">
        <h2>站规掩码日历（七日掩码表世代）</h2>
        <p className="muted">
          热更只追加新世代并前进指针，绝不回溯改写旧段钉扎；旧段核验只读段上钉死位图（口径甲）。
        </p>
        {cal?.current ? (
          <p>
            现行世代 <span className="badge ok">epoch {cal.current.epoch}</span>{" "}
            位图 <span className="mono">{cal.current.bitmap}</span>
            {cal.current.note && <span className="muted"> · {cal.current.note}</span>}
          </p>
        ) : (
          <p className="error">站规掩码日历世代缺失</p>
        )}
        <table>
          <thead><tr><th>epoch</th><th>七日位图</th><th>备注</th><th>落账时刻</th></tr></thead>
          <tbody>
            {(cal?.epochs || []).map((e) => (
              <tr key={e.epoch}>
                <td className="mono">{e.epoch}</td>
                <td className="mono">{e.bitmap}</td>
                <td>{e.note || "—"}</td>
                <td className="mono">{e.created_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="actions" style={{ marginTop: "0.8rem" }}>
          <input
            value={bitmap}
            onChange={(e) => setBitmap(e.target.value)}
            placeholder="新七日位图（7 位 0/1，如 1111111）"
            style={{ minWidth: 240, padding: "0.4rem 0.6rem", borderRadius: 8, border: "1px solid var(--line)", background: "#0c2226", color: "var(--text)" }}
          />
          <input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="备注（可选）"
            style={{ minWidth: 200, padding: "0.4rem 0.6rem", borderRadius: 8, border: "1px solid var(--line)", background: "#0c2226", color: "var(--text)" }}
          />
          <button onClick={hotUpdate} disabled={!bitmap.trim()}>热更掩码日历</button>
        </div>
        {calMsg && <pre className="msg">{calMsg}</pre>}
      </div>
      <div className="panel">
        <h2>空桩清单</h2>
        <ul>
          {stubs.map((s) => (
            <li key={s} className="mono">{s}</li>
          ))}
        </ul>
      </div>
    </>
  );
}
