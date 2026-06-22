// Token Burn — Übersicht widget.
// Drag/resize (S·M·L) · transparency · click to open the dashboard.
// The accent color follows whatever you pick in the dashboard (Overview → Accent color).
// Size / position / transparency are remembered on this computer.
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
export const refreshFrequency = 30000;  // also how fast the widget picks up a dashboard color change

const SIZES = { S: 190, M: 272, L: 334 };
const DEF = { bg: "#f4f3ee", accent: "#9c2a2c", opacity: 0.94 };  // bg = dashboard "skin"

export const className = `
  top: 620px; left: 28px; right: auto; width: 272px;
  color: var(--w-ink, #1a1a17);
  background: rgba(244,243,238,0.94);
  backdrop-filter: blur(22px) saturate(150%); -webkit-backdrop-filter: blur(22px) saturate(150%);
  font-family: -apple-system, "Helvetica Neue", sans-serif;
  border: 1px solid var(--w-line, rgba(0,0,0,0.12)); border-radius: 14px;
  box-shadow: 0 10px 34px rgba(40,30,30,0.16); padding: 13px 15px 11px;
  -webkit-font-smoothing: antialiased;
  a { color: inherit; text-decoration: none; display: block; cursor: grab; }
  .hd { display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid var(--w-line, rgba(0,0,0,0.12)); padding-bottom:6px; margin-bottom:8px; }
  .hdl { display:flex; align-items:center; gap:8px; }
  .lg { width:21px; height:21px; border-radius:5px; background:var(--w-accent,#9c2a2c); color:var(--w-accent-ink,#e7d5a1); display:flex; align-items:center; justify-content:center; flex:none; box-shadow:0 0 0 1px rgba(0,0,0,0.12) inset; }
  .h1 { font-size:14px; font-weight:680; letter-spacing:-0.01em; }
  .exp { font-size:10px; color:var(--w-muted, #9a8a86); }
  .big { font-size:27px; font-weight:700; font-variant-numeric:tabular-nums; line-height:1.1; }
  .big .lbl { font-size:12px; font-weight:500; color:var(--w-muted, #9a8a86); }
  .bars { margin:8px 0 4px; }
  .brow { display:flex; align-items:center; font-size:12px; margin:3px 0; }
  .brow .dot { width:8px; height:8px; border-radius:50%; margin-right:6px; flex:none; }
  .brow .bv { margin-left:auto; font-variant-numeric:tabular-nums; font-weight:600; }
  .sec { font-size:9.5px; text-transform:uppercase; letter-spacing:0.1em; color:var(--w-muted, #9a8a86); margin:10px 0 4px; border-top:1px solid var(--w-line, rgba(0,0,0,0.08)); padding-top:7px; }
  .wrow { display:flex; font-size:11.5px; margin:3px 0; line-height:1.25; }
  .wt { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .wv { margin-left:auto; padding-left:8px; color:var(--w-muted, #7a6f6c); font-variant-numeric:tabular-nums; }
  .ft { font-size:10px; color:var(--w-muted, #9a8a86); margin-top:9px; border-top:1px solid var(--w-line, rgba(0,0,0,0.08)); padding-top:6px; }
  .muted { color:var(--w-muted, #9a8a86); font-size:12px; font-style:italic; }

  &[data-sz="S"] .bars, &[data-sz="S"] .sec, &[data-sz="S"] .wrow, &[data-sz="S"] .ft { display:none; }
  &[data-sz="M"] .sec, &[data-sz="M"] .wrow { display:none; }

  .ctrls { position:absolute; top:8px; right:9px; display:flex; flex-direction:column; gap:5px;
    opacity:0; transition:opacity .15s; background:rgba(255,255,255,0.96); color:#333;
    border:1px solid rgba(0,0,0,0.10); border-radius:10px; padding:6px 7px; box-shadow:0 6px 20px rgba(0,0,0,0.18); z-index:5; }
  &:hover .ctrls { opacity:1; }
  .ctrls .row { display:flex; gap:3px; align-items:center; }
  .ctrls button { font:inherit; font-size:10px; font-weight:700; border:none; background:rgba(var(--w-accent-rgb,156,42,44),0.13); color:var(--w-accent,#9c2a2c);
    min-width:18px; height:18px; border-radius:5px; cursor:pointer; display:flex; align-items:center; justify-content:center; padding:0 4px; line-height:1; }
  .ctrls .sizes button.on { background:var(--w-accent,#9c2a2c); color:var(--w-accent-ink,#fff); }
  .ctrls .arr button, .ctrls .rst { background:#ecebe9; color:#555; }
  .ctrls input[type=range] { -webkit-appearance:none; appearance:none; width:104px; height:14px; background:transparent; cursor:pointer; }
  .ctrls input[type=range]::-webkit-slider-runnable-track { height:2px; border-radius:2px; background:#1a1a17; }
  .ctrls input[type=range]::-webkit-slider-thumb { -webkit-appearance:none; appearance:none; width:11px; height:11px; border-radius:50%; background:#1a1a17; margin-top:-4.5px; box-shadow:0 1px 3px rgba(0,0,0,0.35); }
  .ctrls .cap { font-size:8px; font-weight:700; letter-spacing:0.06em; text-transform:uppercase; color:#999; width:30px; flex:none; }
`;

const fmt = n => { n=+n||0;
  return n>=1e9?(n/1e9).toFixed(2)+"B":n>=1e6?(n/1e6).toFixed(0)+"M":n>=1e3?(n/1e3).toFixed(0)+"K":""+Math.round(n); };
const TOOLS = [["Claude Code","#d4663a"],["Cowork","#6e56cf"],["Codex","#10a37f"]];

const ls = (k,d)=>{ try{ const v=localStorage.getItem(k); return v===null?d:v; }catch(e){ return d; } };
let _off = (()=>{ try{return JSON.parse(localStorage.getItem("tbWidgetOff"))||{x:0,y:0}}catch(e){return {x:0,y:0}} })();
let _sz  = ls("tbWidgetSize","M");
let _opacity = parseFloat(ls("tbWidgetOpacity", DEF.opacity)) || DEF.opacity;
let _bg = DEF.bg;            // card background = the dashboard "skin"
let _accent = DEF.accent;   // follows the dashboard's chosen accent (from the data feed)
let _root=null, _moved=false;

const hexToRgb = h => { h=(h||"").replace("#",""); if(h.length===3) h=h.split("").map(c=>c+c).join(""); const n=parseInt(h||"f4f3ee",16); return [(n>>16)&255,(n>>8)&255,n&255]; };
const lum = ([r,g,b]) => (0.299*r + 0.587*g + 0.114*b) / 255;
const save = ()=>{ try{ localStorage.setItem("tbWidgetOff",JSON.stringify(_off)); localStorage.setItem("tbWidgetSize",_sz); localStorage.setItem("tbWidgetOpacity",String(_opacity)); }catch(e){} };
const moveOnly = ()=>{ if(_root) _root.style.transform = `translate(${_off.x}px, ${_off.y}px)`; };
const applyTheme = ()=>{ if(!_root) return;
  const rgb=hexToRgb(_bg); const dark=lum(rgb)<0.55;
  _root.style.background = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${_opacity})`;
  _root.style.setProperty("--w-ink", dark ? "#f4f1ed" : "#1a1a17");
  _root.style.setProperty("--w-muted", dark ? "rgba(255,255,255,0.62)" : "rgba(0,0,0,0.46)");
  _root.style.setProperty("--w-line", dark ? "rgba(255,255,255,0.16)" : "rgba(0,0,0,0.12)");
  _root.style.borderColor = dark ? "rgba(255,255,255,0.18)" : "rgba(0,0,0,0.10)";
  _root.style.setProperty("--w-accent", _accent);
  var ar=hexToRgb(_accent); _root.style.setProperty("--w-accent-rgb", ar[0]+","+ar[1]+","+ar[2]);
  _root.style.setProperty("--w-accent-ink", lum(hexToRgb(_accent)) < 0.55 ? "#f4eee6" : "#2a1a14");
};
const applyAll = ()=>{ if(!_root) return;
  _root.style.transform = `translate(${_off.x}px, ${_off.y}px)`;
  _root.style.width = SIZES[_sz] + "px";
  _root.dataset.sz = _sz;
  _root.querySelectorAll(".ctrls .sizes button").forEach(b=>b.classList.toggle("on", b.dataset.sz===_sz));
  applyTheme();
};
const grabRoot = (el)=>{ if(!el) return; let w=el.parentElement;
  while(w && w!==document.body){ const p=getComputedStyle(w).position; if(p==="absolute"||p==="fixed"){ _root=w; applyAll(); return; } w=w.parentElement; }
  _root = el.parentElement || el; applyAll();
};
const startDrag = (e)=>{ if(e.button!==0) return;
  const sx=e.clientX, sy=e.clientY, ox=_off.x, oy=_off.y; _moved=false;
  const mv=(ev)=>{ const dx=ev.clientX-sx, dy=ev.clientY-sy; if(Math.abs(dx)+Math.abs(dy)>3) _moved=true; _off={x:ox+dx,y:oy+dy}; moveOnly(); };
  const up=()=>{ document.removeEventListener("mousemove",mv); document.removeEventListener("mouseup",up); document.body.style.userSelect=""; save(); };
  document.body.style.userSelect="none"; document.addEventListener("mousemove",mv); document.addEventListener("mouseup",up);
};
const onClickCard = (e)=>{ if(_moved){ e.preventDefault(); _moved=false; } };
const stopDown = (e)=>{ e.stopPropagation(); };
const halt = (e)=>{ e.preventDefault(); e.stopPropagation(); };
const setSz = (e,s)=>{ halt(e); _sz=s; save(); applyAll(); };
const nudge = (e,dx,dy)=>{ halt(e); _off={x:_off.x+dx,y:_off.y+dy}; save(); moveOnly(); };
const setOpacity = (v)=>{ _opacity=Math.max(0.15,Math.min(1,v)); save(); applyTheme(); };
const resetLayout = (e)=>{ halt(e); _off={x:0,y:0}; _sz="M"; _opacity=DEF.opacity; save(); applyAll();
  if(_root){ const ri=_root.querySelector(".ctrls input[type=range]"); if(ri) ri.value=Math.round(_opacity*100); } };

const Controls = (
  <div className="ctrls" onMouseDown={stopDown}>
    <div className="row sizes">
      <span className="cap">Size</span>
      <button data-sz="S" onMouseDown={stopDown} onClick={(e)=>setSz(e,"S")}>S</button>
      <button data-sz="M" onMouseDown={stopDown} onClick={(e)=>setSz(e,"M")}>M</button>
      <button data-sz="L" onMouseDown={stopDown} onClick={(e)=>setSz(e,"L")}>L</button>
      <button className="rst" onMouseDown={stopDown} onClick={resetLayout} title="Reset position & size">⟲</button>
    </div>
    <div className="row arr">
      <span className="cap">Move</span>
      <button onMouseDown={stopDown} onClick={(e)=>nudge(e,0,-18)}>↑</button>
      <button onMouseDown={stopDown} onClick={(e)=>nudge(e,0,18)}>↓</button>
      <button onMouseDown={stopDown} onClick={(e)=>nudge(e,-18,0)}>←</button>
      <button onMouseDown={stopDown} onClick={(e)=>nudge(e,18,0)}>→</button>
    </div>
    <div className="row alpha">
      <span className="cap">Fade</span>
      <input type="range" min="20" max="100" defaultValue={Math.round(_opacity*100)} onMouseDown={stopDown} onClick={(e)=>e.stopPropagation()} onChange={(e)=>setOpacity(e.target.value/100)} />
    </div>
  </div>
);

const Logo = (
  <span className="lg"><svg viewBox="0 0 24 24" width="13" height="13"><rect x="2.6" y="2.6" width="18.8" height="18.8" rx="1.7" fill="none" stroke="currentColor" stroke-width="2.4"/><path d="M9 3.6V20.4M15.4 3.6V13M9 13H20.4M15.4 8.2H20.4" stroke="currentColor" stroke-width="2.4" fill="none"/></svg></span>
);

export const render = ({ output }) => {
  let d = {};
  try { d = JSON.parse(output); } catch (e) {
    return <a href="http://localhost:8799" ref={grabRoot} onMouseDown={startDrag} onClick={onClickCard}>{Controls}<div className="hd"><span className="hdl">{Logo}<span className="h1">Token Burn</span></span></div><div className="muted">loading…</div></a>;
  }
  if (/^#[0-9a-fA-F]{6}$/.test(d.accent || "")) _accent = d.accent;     // Secondary (highlight) from the dashboard
  _bg = /^#[0-9a-fA-F]{6}$/.test(d.primary || "") ? d.primary : DEF.bg; // Primary (background) from the dashboard
  if (_root) setTimeout(applyTheme, 0);                                 // re-apply on every refresh
  if (d.loading) {
    return <a href="http://localhost:8799" ref={grabRoot} onMouseDown={startDrag} onClick={onClickCard}>{Controls}<div className="hd"><span className="hdl">{Logo}<span className="h1">Token Burn</span></span></div><div className="muted">scanning your logs…</div></a>;
  }
  const tb = d.todayByTool || {};
  const why = (d.topWhy || []).slice(0, 3);
  return (
    <a href="http://localhost:8799" ref={grabRoot} onMouseDown={startDrag} onClick={onClickCard}>
      {Controls}
      <div className="hd"><span className="hdl">{Logo}<span className="h1">Token Burn</span></span><span className="exp">open ⤢</span></div>
      <div className="big">{fmt(d.today)} <span className="lbl">today</span></div>
      <div className="bars">
        {TOOLS.map(([k, c]) => tb[k] ? (
          <div className="brow" key={k}><span className="dot" style={{ background: c }}></span>{k}<span className="bv">{fmt(tb[k])}</span></div>
        ) : null)}
      </div>
      <div className="sec">Why — top sessions</div>
      {why.length ? why.map((w, i) => (
        <div className="wrow" key={i}><span className="wt" title={w[0]}>{w[0]}</span><span className="wv">{fmt(w[3])}</span></div>
      )) : <div className="wrow muted">—</div>}
      <div className="ft">all-time {fmt(d.grand)} · last 7d {fmt(d.week)} · hover to resize / move</div>
    </a>
  );
};
