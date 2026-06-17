// Token Burn — Übersicht widget. Small card; click to open the full dashboard.
// It also keeps the local tracker server alive so the dashboard is always there.
export const command = `
export PATH=/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin
DIR="__TRACKER_DIR__"
PY="$(command -v python3 || echo /usr/bin/python3)"
OUT="$(curl -s --max-time 2 http://localhost:8799/api/summary 2>/dev/null)"
if [ -z "$OUT" ]; then
  ( cd "$DIR" && nohup "$PY" tracker.py >/dev/null 2>&1 & )
  OUT='{"loading":true}'
fi
echo "$OUT"
`;
export const refreshFrequency = 180000;
export const className = `
  top: 620px; left: 28px; right: auto; width: 268px;
  background: rgba(230,231,248,0.93); color: #1a1a17;
  backdrop-filter: blur(22px) saturate(160%); -webkit-backdrop-filter: blur(22px) saturate(160%);
  font-family: -apple-system, "Helvetica Neue", sans-serif;
  border: 1px solid rgba(255,255,255,0.55); border-radius: 12px;
  box-shadow: 0 10px 34px rgba(0,0,0,0.14); padding: 14px 16px 12px;
  -webkit-font-smoothing: antialiased;
  a { color: inherit; text-decoration: none; display: block; }
  .hd { display:flex; justify-content:space-between; align-items:baseline; border-bottom:1px solid #1a1a17; padding-bottom:6px; margin-bottom:8px; }
  .h1 { font-size:14px; font-weight:650; letter-spacing:-0.01em; }
  .exp { font-size:10px; color:#8a8a7c; }
  .big { font-size:26px; font-weight:680; font-variant-numeric:tabular-nums; line-height:1.1; }
  .big .lbl { font-size:12px; font-weight:500; color:#8a8a7c; }
  .bars { margin:8px 0 4px; }
  .brow { display:flex; align-items:center; font-size:12px; margin:3px 0; }
  .brow .dot { width:8px; height:8px; border-radius:50%; margin-right:6px; }
  .brow .bv { margin-left:auto; font-variant-numeric:tabular-nums; font-weight:600; }
  .sec { font-size:9.5px; text-transform:uppercase; letter-spacing:0.1em; color:#8a8a7c; margin:10px 0 4px; border-top:1px solid #e2e1d4; padding-top:7px; }
  .wrow { display:flex; font-size:11.5px; margin:3px 0; line-height:1.25; }
  .wt { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .wv { margin-left:auto; padding-left:8px; color:#6b6b60; font-variant-numeric:tabular-nums; }
  .ft { font-size:10px; color:#9a9a8c; margin-top:9px; border-top:1px solid #e2e1d4; padding-top:6px; }
  .muted { color:#9a9a8c; font-size:12px; font-style:italic; }
`;
const fmt = n => { n=+n||0;
  return n>=1e9?(n/1e9).toFixed(2)+"B":n>=1e6?(n/1e6).toFixed(0)+"M":n>=1e3?(n/1e3).toFixed(0)+"K":""+Math.round(n); };
const TOOLS = [["Claude Code","#d4663a"],["Cowork","#6e56cf"],["Codex","#10a37f"]];
export const render = ({ output }) => {
  let d = {};
  try { d = JSON.parse(output); } catch (e) {
    return <div><div className="hd"><span className="h1">Token Burn</span></div><div className="muted">loading…</div></div>;
  }
  if (d.loading) {
    return <div><div className="hd"><span className="h1">Token Burn</span></div><div className="muted">scanning your logs…</div></div>;
  }
  const tb = d.todayByTool || {};
  const why = (d.topWhy || []).slice(0, 3);
  return (
    <a href="http://localhost:8799">
      <div className="hd"><span className="h1">Token Burn</span><span className="exp">open ⤢</span></div>
      <div className="big">{fmt(d.today)} <span className="lbl">today</span></div>
      <div className="bars">
        {TOOLS.map(([k, c]) => tb[k] ? (
          <div className="brow" key={k}><span className="dot" style={{ background: c }}></span>{k}<span className="bv">{fmt(tb[k])}</span></div>
        ) : null)}
      </div>
      <div className="sec">Why — top sessions</div>
      {why.length ? why.map((w, i) => (
        <div className="wrow" key={i}><span className="wt" title={w[0]}>{w[0]}</span><span className="wv">{fmt(w[3])}</span></div>
      )) : <div className="muted">—</div>}
      <div className="ft">all-time {fmt(d.grand)} · last 7d {fmt(d.week)} · click to expand</div>
    </a>
  );
};
