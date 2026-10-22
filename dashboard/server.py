"""Monitoring Dashboard (component 7), standard library only.

    python -m dashboard.server --db volatiledns.sqlite --port 8601 --control-port 5302

Serves one web page (http://127.0.0.1:8601/) that refreshes every 2 s and shows the state of the test DNS
server with buttons for fault injection, the decision counters, the cache with remaining TTLs, every
record's history with the estimated change rate and the stale limit the VolatileDNS policy would give it,
and the latest decisions.  It only reads the SQLite file written by the proxy, so it runs as a separate
process.
"""
import argparse
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import log

from testbed import controller

ALPHA, BETA, EPS, CAP = 1.0, 3600.0, 0.05, 1200.0     # defaults of volatiledns.config


def read_state(db_path: str, control_port: int) -> dict:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        dec = con.execute("select id, ts, key, action, reason, stale_age, stale_limit, policy "
                          "from decisions order by id").fetchall()
        cache = con.execute("select key, addrs, ttl, expires_at, negative from cache order by key").fetchall()
        hist = con.execute("select key, ts, real_change from history order by id").fetchall()
    finally:
        con.close()
    now = max((d[1] for d in dec), default=0.0)
    counts = {}
    for d in dec:
        counts[d[3]] = counts.get(d[3], 0) + 1
    ttl_of = {c[0]: c[2] for c in cache}
    per = {}
    for key, ts, ch in hist:
        e = per.setdefault(key, dict(first=ts, last=ts, n=0, changes=0))
        e["last"] = ts
        e["n"] += 1
        e["changes"] += ch
    window, bucket = 120.0, 2.0
    nb = int(window / bucket)
    series = [{k: 0 for k in ("cache", "upstream", "stale", "servfail", "nxdomain")} for _ in range(nb)]
    for d in dec:
        age = now - d[1]
        if 0 <= age < window and d[3] in series[0]:
            series[nb - 1 - int(age // bucket)][d[3]] += 1
    estimates = []
    for key, e in sorted(per.items()):
        T = e["last"] - e["first"]
        lam = (ALPHA + e["changes"]) / (BETA + T)
        limit = max(-1.0, min(CAP, -log(1 - EPS) / lam - ttl_of.get(key, 0)))
        estimates.append(dict(key=key, observations=e["n"], real_changes=e["changes"], watched_s=round(T, 1),
                              changes_per_hour=round(lam * 3600, 3), stale_limit_s=round(limit, 1)))
    try:
        server = controller.send("status", port=control_port, timeout=1)
    except OSError:
        server = "unreachable"
    return dict(
        server=server, counts=counts, total=len(dec), series=series,
        policy=dec[-1][7] if dec else "-",
        cache=[dict(key=c[0], addrs=c[1], ttl=c[2], remaining=round(c[3] - now, 1),
                    state="fresh" if c[3] - now > 0 else "expired", negative=bool(c[4])) for c in cache],
        estimates=estimates,
        decisions=[dict(id=d[0], t=round(d[1] - (dec[0][1] if dec else 0), 2), key=d[2], action=d[3], reason=d[4],
                        stale_age=None if d[5] is None else round(d[5], 1),
                        limit=None if d[6] is None else round(d[6], 1), policy=d[7]) for d in dec[-100:][::-1]])


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VolatileDNS | Resilience Console</title>
<style>
:root{
  --bg:#f4f6fa;--card:#ffffff;--ink:#0f172a;--ink2:#334155;--muted:#64748b;--line:#e2e8f0;--soft:#f8fafc;
  --primary:#3b5bdb;--cache:#3b82f6;--upstream:#10b981;--stale:#f59e0b;--servfail:#ef4444;--nxdomain:#8b5cf6;
  --bar:#0b1220;--shadow:0 1px 2px rgba(15,23,42,.05),0 4px 14px rgba(15,23,42,.05);
}
@media (prefers-color-scheme:dark){
  :root{--bg:#0b1020;--card:#121a2c;--ink:#e8edf6;--ink2:#c3cddd;--muted:#8a97ad;--line:#222d44;--soft:#0f1627;
    --bar:#070b16;--shadow:0 1px 2px rgba(0,0,0,.4),0 6px 20px rgba(0,0,0,.3)}
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 "Segoe UI",system-ui,-apple-system,Roboto,Helvetica,Arial,sans-serif}
.bar{background:var(--bar);color:#e8edf6;border-bottom:1px solid rgba(255,255,255,.06)}
.bar-in{max-width:1240px;margin:0 auto;padding:12px 22px;display:flex;align-items:center;gap:18px}
.brand{display:flex;align-items:center;gap:11px;font-weight:700;font-size:17px;letter-spacing:-.01em}
.brand small{display:block;font-weight:500;font-size:11px;color:#8fa0bd;letter-spacing:.04em;text-transform:uppercase;margin-top:-2px}
.logo{width:32px;height:32px;border-radius:8px;background:linear-gradient(135deg,#3b5bdb,#10b981);display:grid;place-items:center}
.spacer{flex:1}
.meta{display:flex;gap:8px;flex-wrap:wrap}
.tag{font-size:12px;border:1px solid rgba(255,255,255,.14);color:#c3cddd;padding:3px 10px;border-radius:999px}
.tag b{color:#fff;font-weight:600}
.live{display:flex;align-items:center;gap:8px;font-size:12px;color:#9fb0cc}
.dot{width:8px;height:8px;border-radius:50%;background:#10b981;box-shadow:0 0 0 0 rgba(16,185,129,.6);animation:pulse 2s infinite}
.dot.off{background:#ef4444;animation:none}
@keyframes pulse{70%{box-shadow:0 0 0 7px rgba(16,185,129,0)}100%{box-shadow:0 0 0 0 rgba(16,185,129,0)}}
.wrap{max-width:1240px;margin:0 auto;padding:22px 22px 44px}
.title{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;margin-bottom:16px;flex-wrap:wrap}
.title h1{margin:0;font-size:22px;letter-spacing:-.015em}
.title p{margin:2px 0 0;color:var(--muted);font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow)}
.row{display:grid;gap:16px;margin-bottom:16px}
.r1{grid-template-columns:330px minmax(0,1fr) 310px;align-items:start}
@media (max-width:1180px){.r1{grid-template-columns:1fr}}
.lbl{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);font-weight:600}
.srv{padding:18px 20px}
.state{display:flex;align-items:center;gap:12px;margin:8px 0 12px}
.pill{display:inline-flex;align-items:center;gap:8px;padding:5px 14px;border-radius:8px;font-weight:700;font-size:17px;letter-spacing:.03em}
.pill i{width:9px;height:9px;border-radius:50%;background:currentColor;display:inline-block}
.pill.up{background:rgba(16,185,129,.13);color:#059669}
.pill.down{background:rgba(239,68,68,.13);color:#dc2626}
.pill.unk{background:rgba(100,116,139,.15);color:var(--muted)}
.facts{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:14px}
.fact{background:var(--soft);border:1px solid var(--line);border-radius:8px;padding:7px 10px}
.fact b{display:block;font-size:16px;font-variant-numeric:tabular-nums}.fact span{font-size:11px;color:var(--muted)}
.btns{display:flex;gap:8px}
button{font:inherit;font-weight:600;font-size:13px;border:1px solid var(--line);background:var(--card);color:var(--ink);
  padding:8px 14px;border-radius:8px;cursor:pointer;transition:background .15s,box-shadow .15s,transform .05s}
button:hover{box-shadow:0 2px 8px rgba(15,23,42,.15)}button:active{transform:translateY(1px)}
.btns button{flex:1}
button.danger{background:#ef4444;border-color:#ef4444;color:#fff}
button.ok{background:#10b981;border-color:#10b981;color:#fff}
button.primary{background:var(--primary);border-color:var(--primary);color:#fff}
.mv{margin-top:14px;padding-top:14px;border-top:1px solid var(--line)}
.mv .f{display:flex;gap:8px;margin-top:8px;flex-wrap:wrap}
select,input{font:inherit;font-size:13px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink);min-width:0}
.kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:16px}
@media (max-width:1100px){.kpis{grid-template-columns:repeat(2,1fr)}}
.kpis{margin-bottom:16px}
.kpi{padding:16px 18px;position:relative;overflow:hidden}
.kpi::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--c)}
.kpi .n{font-size:30px;font-weight:700;letter-spacing:-.02em;line-height:1.15;margin-top:4px;font-variant-numeric:tabular-nums}
.kpi .s{font-size:12px;color:var(--muted);margin-top:2px}
.kpi .m{height:5px;border-radius:9px;background:var(--line);margin-top:10px;overflow:hidden}
.kpi .m i{display:block;height:100%;background:var(--c);border-radius:9px;transition:width .4s}
.ch{padding:16px 20px 12px}
.ch-h{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:6px}
.ch-h h2,.sh h2{margin:0;font-size:15px;font-weight:650}
.ch-h span,.sh span.sub{color:var(--muted);font-size:12px}
svg{display:block;width:100%;height:auto}
.legend{display:flex;gap:16px;flex-wrap:wrap;margin-top:8px;font-size:12px;color:var(--ink2)}
.legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:-1px}
.mix{padding:16px 20px}
.stack{display:flex;height:16px;border-radius:9px;overflow:hidden;background:var(--line);margin:12px 0 14px}
.stack i{display:block;height:100%;transition:width .4s}
.mrow{display:flex;align-items:center;justify-content:space-between;padding:7px 0;border-top:1px solid var(--line);font-size:13px}
.mrow:first-of-type{border-top:0}
.mrow span:first-child{display:flex;align-items:center;gap:8px}
.mrow i{width:10px;height:10px;border-radius:3px;display:inline-block}
.mrow b{font-variant-numeric:tabular-nums}.mrow em{font-style:normal;color:var(--muted);margin-left:8px;font-size:12px}
section{margin-bottom:16px}
.sh{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px 20px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.tools{display:flex;gap:8px}
.tools select,.tools input{padding:6px 9px;font-size:12.5px}
.tw{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}
th{background:var(--soft);color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.07em;text-align:left;
  padding:9px 20px;font-weight:600;white-space:nowrap;border-bottom:1px solid var(--line)}
td{padding:9px 20px;border-bottom:1px solid var(--line);white-space:nowrap;color:var(--ink2)}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover td{background:var(--soft)}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px;color:var(--ink)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.badge{display:inline-flex;align-items:center;gap:6px;padding:2px 10px;border-radius:6px;font-size:11.5px;font-weight:650;
  color:var(--c);background:color-mix(in srgb,var(--c) 13%,transparent)}
.badge::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--c)}
.b-cache{--c:var(--cache)}.b-upstream{--c:var(--upstream)}.b-stale{--c:var(--stale)}
.b-servfail{--c:var(--servfail)}.b-nxdomain{--c:var(--nxdomain)}.b-fresh{--c:#10b981}
.b-expired{--c:#94a3b8}.b-neg{--c:var(--nxdomain)}.b-none{--c:var(--servfail)}
.lim{display:flex;align-items:center;gap:12px;min-width:230px}
.tr{flex:1;height:7px;border-radius:9px;background:var(--line);overflow:hidden}
.fl{height:100%;border-radius:9px;background:linear-gradient(90deg,#3b5bdb,#10b981)}
.empty{padding:26px 20px;color:var(--muted);text-align:center}
footer{color:var(--muted);font-size:12px;text-align:center;padding-top:6px}
</style></head>
<body>
<div class="bar"><div class="bar-in">
  <div class="brand">
    <div class="logo"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.6 2.8 4 5.8 4 9s-1.4 6.2-4 9c-2.6-2.8-4-5.8-4-9s1.4-6.2 4-9z"/></svg></div>
    <div>VolatileDNS<small>Resilience console</small></div>
  </div>
  <div class="spacer"></div>
  <div class="meta"><span class="tag">Policy <b id="policy">-</b></span><span class="tag">Decisions <b id="total">0</b></span></div>
  <div class="live"><span id="dot" class="dot"></span><span id="upd">connecting...</span></div>
</div></div>

<div class="wrap">
  <div class="title"><div><h1>Overview</h1>
  <p>DNS resilience monitoring dashboard: cache, learned change rates and every stale-answer decision of the proxy.</p></div></div>

  <div class="kpis" id="kpis"></div>

  <div class="row r1">
    <div class="card srv">
      <div class="lbl">Upstream DNS server</div>
      <div class="state"><span id="pill" class="pill unk"><i></i><span id="pilltxt">UNKNOWN</span></span></div>
      <div class="facts" id="facts"></div>
      <div class="btns">
        <button class="danger" onclick="cmd('down')">Take down</button>
        <button class="ok" onclick="cmd('up')">Bring up</button>
      </div>
      <div class="mv">
        <div class="lbl">Move a service to a new IP</div>
        <div class="f">
          <select id="mname"><option>lab.college.test</option><option>portal.college.test</option><option>session.college.test</option></select>
          <input id="mip" value="172.28.0.99" size="12">
          <button class="primary" onclick="cmd('set '+mname.value+' '+mip.value+' 60')">Move</button>
        </div>
      </div>
    </div>
    <div class="card ch">
      <div class="ch-h"><h2>Decision timeline</h2><span>last 2 minutes, 2 s buckets</span></div>
      <div id="chart"></div>
      <div class="legend" id="legend"></div>
    </div>
    <div class="card mix">
      <div class="ch-h"><h2>Decision mix</h2><span id="mixn"></span></div>
      <div class="stack" id="stack"></div>
      <div id="mixrows"></div>
    </div>
  </div>

  <section><div class="card">
    <div class="sh"><h2>Cache</h2><span class="sub" id="cache-n"></span></div>
    <div class="tw"><table id="cache"></table></div>
  </div></section>

  <section><div class="card">
    <div class="sh"><h2>Record history and volatility estimate</h2><span class="sub">stale limit under the VolatileDNS policy (cap 1200 s)</span></div>
    <div class="tw"><table id="est"></table></div>
  </div></section>

  <section><div class="card">
    <div class="sh"><h2>Decision log</h2>
      <div class="tools"><select id="fact" onchange="draw()"><option value="">All actions</option><option>cache</option><option>upstream</option><option>stale</option><option>servfail</option><option>nxdomain</option></select>
      <input id="fq" placeholder="Filter by name" oninput="draw()"></div></div>
    <div class="tw"><table id="dec"></table></div>
  </div></section>

  <footer>VolatileDNS &middot; refreshes every 2 seconds &middot; reads the proxy's SQLite decision log</footer>
</div>

<script>
var CAP=1200, COL={cache:'#3b82f6',upstream:'#10b981',stale:'#f59e0b',nxdomain:'#8b5cf6',servfail:'#ef4444'};
var ORDER=['cache','upstream','stale','nxdomain','servfail'], NAME={cache:'Cache hit',upstream:'From DNS server',stale:'Stale served',nxdomain:'NXDOMAIN',servfail:'SERVFAIL'};
var DESC={cache:'fresh answers from the cache',upstream:'fresh answers from the DNS server',stale:'expired answers served',servfail:'queries refused',nxdomain:'name does not exist'};
var S=null;
function esc(s){return String(s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
function badge(t,c){return '<span class="badge b-'+c+'">'+esc(t)+'</span>'}
function val(v){return v===null||v===undefined?'<span style="color:var(--muted)">&ndash;</span>':esc(v)}
function pct(a,b){return b?(100*a/b):0}
function table(id,head,rows,empty){
  var t=document.getElementById(id);
  if(!rows.length){t.innerHTML='<tbody><tr><td class="empty">'+empty+'</td></tr></tbody>';return}
  t.innerHTML='<thead><tr>'+head.map(function(h){var n=h.slice(-1)=='~';return '<th'+(n?' class="num"':'')+'>'+(n?h.slice(0,-1):h)+'</th>'}).join('')+'</tr></thead><tbody>'+rows.join('')+'</tbody>'}
function cmd(c){fetch('/api/cmd',{method:'POST',body:c}).then(load)}

function chart(series){
  var W=560,H=250,L=30,B=26,T=8,R=4,n=series.length,pw=W-L-R,ph=H-T-B,bw=pw/n;
  var mx=1; series.forEach(function(b){var t=0;ORDER.forEach(function(k){t+=b[k]||0});if(t>mx)mx=t});
  var step=mx<=4?1:Math.ceil(mx/4); mx=Math.ceil(mx/step)*step;
  var g='';
  for(var v=0;v<=mx;v+=step){var y=T+ph-ph*v/mx;
    g+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+y+'" y2="'+y+'" stroke="var(--line)"/><text x="'+(L-6)+'" y="'+(y+4)+'" text-anchor="end" font-size="11" fill="var(--muted)">'+v+'</text>'}
  var bars='';
  series.forEach(function(b,i){var y=T+ph;
    ORDER.forEach(function(k){var c=b[k]||0;if(!c)return;var h=ph*c/mx;y-=h;
      bars+='<rect x="'+(L+i*bw+1)+'" y="'+y+'" width="'+Math.max(1,bw-2)+'" height="'+h+'" rx="1.5" fill="'+COL[k]+'"/>'})});
  var lab=[[0,'-120 s'],[Math.floor(n/2),'-60 s'],[n-1,'now']].map(function(p){
    return '<text x="'+(L+p[0]*bw+(p[0]==n-1?bw:0))+'" y="'+(H-6)+'" text-anchor="'+(p[0]==0?'start':(p[0]==n-1?'end':'middle'))+'" font-size="11" fill="var(--muted)">'+p[1]+'</text>'}).join('');
  document.getElementById('chart').innerHTML='<svg viewBox="0 0 '+W+' '+H+'" role="img" aria-label="Decisions over time">'+g+bars+lab+'</svg>';
  document.getElementById('legend').innerHTML=ORDER.map(function(k){return '<span><i style="background:'+COL[k]+'"></i>'+NAME[k]+'</span>'}).join('');
}

function draw(){
  if(!S)return;
  var s=S, tot=s.total||0;
  var q=document.getElementById('fq').value.toLowerCase(), a=document.getElementById('fact').value;
  var rows=s.decisions.filter(function(d){return (!a||d.action==a)&&(!q||d.key.toLowerCase().indexOf(q)>=0)}).slice(0,40);
  table('dec',['#~','t (s)~','Name / type','Action','Reason','Stale age (s)~','Limit (s)~','Policy'],rows.map(function(d){
    return '<tr><td class="num">'+esc(d.id)+'</td><td class="num">'+esc(d.t)+'</td><td class="mono">'+esc(d.key)+'</td><td>'+badge(NAME[d.action]||d.action,d.action)+
      '</td><td>'+esc(d.reason)+'</td><td class="num">'+val(d.stale_age)+'</td><td class="num">'+val(d.limit)+'</td><td>'+esc(d.policy)+'</td></tr>'}),'No decisions match the filter.');
}

function load(){
  fetch('/api/state').then(function(r){return r.json()}).then(function(s){
    S=s; var tot=s.total||0;
    var down=s.server.indexOf('up=False')>=0, ok=s.server.indexOf('up=True')>=0;
    document.getElementById('pill').className='pill '+(down?'down':(ok?'up':'unk'));
    document.getElementById('pilltxt').textContent=down?'DOWN':(ok?'UP':'UNREACHABLE');
    var kv={};s.server.split(' ').forEach(function(p){var x=p.split('=');if(x.length==2)kv[x[0]]=x[1]});
    document.getElementById('facts').innerHTML=['queries','dropped','names'].map(function(k){
      return '<div class="fact"><b>'+esc(kv[k]===undefined?'-':kv[k])+'</b><span>'+k+'</span></div>'}).join('');
    document.getElementById('policy').textContent=s.policy;
    document.getElementById('total').textContent=tot.toLocaleString();
    document.getElementById('kpis').innerHTML=ORDER.map(function(k){var c=s.counts[k]||0;
      return '<div class="card kpi" style="--c:'+COL[k]+'"><div class="lbl">'+NAME[k]+'</div><div class="n">'+c.toLocaleString()+
        '</div><div class="s">'+DESC[k]+'</div><div class="m"><i style="width:'+pct(c,tot).toFixed(1)+'%"></i></div></div>'}).join('');
    chart(s.series);
    document.getElementById('mixn').textContent=tot.toLocaleString()+' decisions';
    document.getElementById('stack').innerHTML=ORDER.map(function(k){return '<i style="width:'+pct(s.counts[k]||0,tot).toFixed(2)+'%;background:'+COL[k]+'"></i>'}).join('');
    document.getElementById('mixrows').innerHTML=ORDER.map(function(k){var c=s.counts[k]||0;
      return '<div class="mrow"><span><i style="background:'+COL[k]+'"></i>'+NAME[k]+'</span><span><b>'+c.toLocaleString()+'</b><em>'+pct(c,tot).toFixed(1)+'%</em></span></div>'}).join('');
    document.getElementById('cache-n').textContent=s.cache.length+' entries';
    table('cache',['Name / type','Addresses','TTL (s)~','Remaining (s)~','State'],s.cache.map(function(c){
      var st=c.negative?badge('negative','neg'):badge(c.state,c.state);
      return '<tr><td class="mono">'+esc(c.key)+'</td><td class="mono">'+(c.addrs?esc(c.addrs):'&ndash;')+'</td><td class="num">'+esc(c.ttl)+
        '</td><td class="num">'+esc(c.remaining)+'</td><td>'+st+'</td></tr>'}),'The cache is empty.');
    table('est',['Name / type','Observations~','Real changes~','Watched (s)~','Est. changes / hour~','Stale limit'],s.estimates.map(function(e){
      var lim=e.stale_limit_s, w=Math.max(0,Math.min(100,lim/CAP*100));
      var bar=lim<=0?badge('no stale allowance','none'):
        '<div class="lim"><div class="tr"><div class="fl" style="width:'+w+'%"></div></div><span class="mono">'+esc(lim)+' s</span></div>';
      return '<tr><td class="mono">'+esc(e.key)+'</td><td class="num">'+esc(e.observations)+'</td><td class="num">'+esc(e.real_changes)+
        '</td><td class="num">'+esc(e.watched_s)+'</td><td class="num">'+esc(e.changes_per_hour)+'</td><td>'+bar+'</td></tr>'}),'No history yet.');
    draw();
    document.getElementById('dot').className='dot';
    document.getElementById('upd').textContent='Updated '+new Date().toLocaleTimeString();
  }).catch(function(){document.getElementById('dot').className='dot off';document.getElementById('upd').textContent='Connection lost'})
}
load(); setInterval(load,2000);
</script></body></html>
"""


def make_handler(db, control_port):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype):
            b = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path == "/":
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif self.path == "/api/state":
                try:
                    self._send(200, json.dumps(read_state(db, control_port)), "application/json")
                except sqlite3.Error as e:
                    self._send(503, json.dumps({"error": str(e)}), "application/json")
            else:
                self._send(404, "not found", "text/plain")

        def do_POST(self):
            if self.path != "/api/cmd":
                return self._send(404, "not found", "text/plain")
            cmd = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode().strip()
            if cmd.split(" ")[0] not in ("down", "up", "set", "del", "status"):     # only the controller verbs
                return self._send(400, "bad command", "text/plain")
            try:
                self._send(200, controller.send(cmd, port=control_port), "text/plain")
            except OSError as e:
                self._send(502, str(e), "text/plain")
    return H


def serve(db, port=8601, control_port=5302, host="127.0.0.1"):
    httpd = ThreadingHTTPServer((host, port), make_handler(db, control_port))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="volatiledns.sqlite")
    ap.add_argument("--port", type=int, default=8601)
    ap.add_argument("--control-port", type=int, default=5302)
    a = ap.parse_args()
    httpd = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(a.db, a.control_port))
    print(f"dashboard on http://127.0.0.1:{a.port}/", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
