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
        server=server, counts=counts, total=len(dec),
        cache=[dict(key=c[0], addrs=c[1], ttl=c[2], remaining=round(c[3] - now, 1),
                    state="fresh" if c[3] - now > 0 else "expired", negative=bool(c[4])) for c in cache],
        estimates=estimates,
        decisions=[dict(id=d[0], t=round(d[1] - (dec[0][1] if dec else 0), 2), key=d[2], action=d[3], reason=d[4],
                        stale_age=None if d[5] is None else round(d[5], 1),
                        limit=None if d[6] is None else round(d[6], 1), policy=d[7]) for d in dec[-40:][::-1]])


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VolatileDNS dashboard</title>
<style>
:root{
  --bg:#f3f5f9;--card:#ffffff;--ink:#16202e;--muted:#6b778a;--line:#e3e8f0;--head:#f7f9fc;
  --blue:#2f6fed;--green:#1a9b5b;--amber:#e09a12;--red:#d6414f;--purple:#7c5cd6;--grey:#8a94a6;
  --shadow:0 1px 2px rgba(20,32,50,.06),0 6px 18px rgba(20,32,50,.06);
}
@media (prefers-color-scheme:dark){
  :root{--bg:#0e131b;--card:#161d29;--ink:#e6ebf3;--muted:#95a1b6;--line:#263041;--head:#1b2432;
    --shadow:0 1px 2px rgba(0,0,0,.4),0 6px 18px rgba(0,0,0,.35)}
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:22px 18px 40px}
header{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:18px}
h1{margin:0;font-size:24px;letter-spacing:-.01em}
h1 span{color:var(--blue)}
.sub{color:var(--muted);font-size:13px;margin-top:2px}
.live{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:12px}
.dot{width:9px;height:9px;border-radius:50%;background:var(--green);box-shadow:0 0 0 0 rgba(26,155,91,.6);animation:pulse 2s infinite}
.dot.off{background:var(--red);animation:none}
@keyframes pulse{70%{box-shadow:0 0 0 8px rgba(26,155,91,0)}100%{box-shadow:0 0 0 0 rgba(26,155,91,0)}}
.grid{display:grid;gap:14px}
.top{grid-template-columns:minmax(300px,1.35fr) repeat(5,minmax(110px,1fr))}
@media (max-width:980px){.top{grid-template-columns:repeat(2,1fr)}.top .server{grid-column:1/-1}}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow)}
.server{padding:16px 18px}
.label{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);font-weight:600}
.pill{display:inline-flex;align-items:center;gap:8px;margin-top:6px;padding:6px 14px;border-radius:999px;
  font-weight:700;font-size:18px;letter-spacing:.02em}
.pill.up{background:rgba(26,155,91,.14);color:var(--green)}
.pill.down{background:rgba(214,65,79,.14);color:var(--red)}
.pill.unk{background:rgba(138,148,166,.18);color:var(--grey)}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0 12px}
.chip{background:var(--head);border:1px solid var(--line);border-radius:8px;padding:2px 8px;font-size:12px;color:var(--muted)}
.chip b{color:var(--ink)}
.controls{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
button{font:inherit;font-weight:600;border:1px solid var(--line);background:var(--head);color:var(--ink);
  padding:7px 13px;border-radius:9px;cursor:pointer;transition:transform .05s,box-shadow .15s}
button:hover{box-shadow:0 2px 8px rgba(20,32,50,.18)}button:active{transform:translateY(1px)}
button.danger{background:var(--red);border-color:var(--red);color:#fff}
button.ok{background:var(--green);border-color:var(--green);color:#fff}
button.primary{background:var(--blue);border-color:var(--blue);color:#fff}
select,input{font:inherit;padding:7px 9px;border:1px solid var(--line);border-radius:9px;background:var(--card);color:var(--ink)}
.move{margin-top:12px;padding-top:12px;border-top:1px dashed var(--line)}
.stat{padding:14px 16px;border-top:4px solid var(--c);align-self:start}
.stat .n{font-size:30px;font-weight:700;letter-spacing:-.02em;margin-top:2px}
.stat .d{font-size:12px;color:var(--muted)}
section{margin-top:16px}
section>.card{padding:0;overflow:hidden}
.sh{display:flex;align-items:baseline;justify-content:space-between;padding:13px 18px;border-bottom:1px solid var(--line)}
.sh h2{margin:0;font-size:15px}.sh span{color:var(--muted);font-size:12px}
.tw{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}
th{background:var(--head);color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.06em;text-align:left;
  padding:9px 18px;font-weight:600;white-space:nowrap}
td{padding:8px 18px;border-top:1px solid var(--line);white-space:nowrap}
tbody tr:hover td{background:var(--head)}
td.mono,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11.5px;font-weight:700;letter-spacing:.02em;
  color:var(--c);background:color-mix(in srgb,var(--c) 15%,transparent)}
.b-cache{--c:var(--blue)}.b-upstream{--c:var(--green)}.b-stale{--c:var(--amber)}
.b-servfail{--c:var(--red)}.b-nxdomain{--c:var(--purple)}.b-fresh{--c:var(--green)}
.b-expired{--c:var(--grey)}.b-neg{--c:var(--purple)}.b-none{--c:var(--red)}
.bar{display:flex;align-items:center;gap:10px;min-width:210px}
.track{flex:1;height:8px;border-radius:99px;background:var(--line);overflow:hidden}
.fill{height:100%;border-radius:99px;background:linear-gradient(90deg,var(--blue),var(--green))}
.empty{padding:22px 18px;color:var(--muted)}
footer{margin-top:22px;color:var(--muted);font-size:12px;text-align:center}
</style></head>
<body><div class="wrap">
<header>
  <div><h1><span>Volatile</span>DNS monitoring dashboard</h1>
  <div class="sub">Live view of the caching proxy: cache, learned change rates and every stale-answer decision</div></div>
  <div class="live"><span id="dot" class="dot"></span><span id="upd">connecting...</span></div>
</header>

<div class="grid top">
  <div class="card server">
    <div class="label">Test DNS server</div>
    <div id="pill" class="pill unk">?</div>
    <div class="chips" id="chips"></div>
    <div class="controls">
      <button class="danger" onclick="cmd('down')">Take server down</button>
      <button class="ok" onclick="cmd('up')">Bring server up</button>
    </div>
    <div class="controls move">
      <select id="mname"><option>lab.college.test</option><option>portal.college.test</option><option>session.college.test</option></select>
      <input id="mip" value="172.28.0.99" size="13">
      <button class="primary" onclick="cmd('set '+mname.value+' '+mip.value+' 60')">Move service</button>
    </div>
  </div>
  <div class="card stat" style="--c:var(--blue)"><div class="label">Cache hits</div><div class="n" id="n-cache">0</div><div class="d">fresh answers from the cache</div></div>
  <div class="card stat" style="--c:var(--green)"><div class="label">Upstream</div><div class="n" id="n-upstream">0</div><div class="d">fresh from the DNS server</div></div>
  <div class="card stat" style="--c:var(--amber)"><div class="label">Stale</div><div class="n" id="n-stale">0</div><div class="d">expired answers served</div></div>
  <div class="card stat" style="--c:var(--red)"><div class="label">SERVFAIL</div><div class="n" id="n-servfail">0</div><div class="d">refused to answer</div></div>
  <div class="card stat" style="--c:var(--purple)"><div class="label">NXDOMAIN</div><div class="n" id="n-nxdomain">0</div><div class="d">name does not exist</div></div>
</div>

<section><div class="card">
  <div class="sh"><h2>Cache</h2><span id="cache-n"></span></div>
  <div class="tw"><table id="cache"></table></div>
</div></section>

<section><div class="card">
  <div class="sh"><h2>Record history and volatility estimate</h2><span>stale limit under the VolatileDNS policy (cap 1200 s)</span></div>
  <div class="tw"><table id="est"></table></div>
</div></section>

<section><div class="card">
  <div class="sh"><h2>Latest decisions</h2><span>newest first, last 40</span></div>
  <div class="tw"><table id="dec"></table></div>
</div></section>

<footer>VolatileDNS &middot; refreshes every 2 seconds &middot; reads the proxy's SQLite log</footer>
</div>
<script>
var CAP=1200;
function esc(s){return String(s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
function badge(t,cls){return '<span class="badge b-'+cls+'">'+esc(t)+'</span>'}
function val(v){return v===null||v===undefined?'<span style="color:var(--muted)">-</span>':esc(v)}
function table(id,head,rows,empty){
  var t=document.getElementById(id);
  if(!rows.length){t.innerHTML='<tbody><tr><td class="empty">'+empty+'</td></tr></tbody>';return}
  t.innerHTML='<thead><tr>'+head.map(function(h){var n=h.slice(-1)=='~';return '<th'+(n?' class="num"':'')+'>'+(n?h.slice(0,-1):h)+'</th>'}).join('')+'</tr></thead><tbody>'+rows.join('')+'</tbody>'}
function cmd(c){fetch('/api/cmd',{method:'POST',body:c}).then(load)}
function load(){
  fetch('/api/state').then(function(r){return r.json()}).then(function(s){
    var down=s.server.indexOf('up=False')>=0, ok=s.server.indexOf('up=True')>=0;
    var pill=document.getElementById('pill');
    pill.className='pill '+(down?'down':(ok?'up':'unk'));
    pill.textContent=down?'DOWN':(ok?'UP':'UNREACHABLE');
    var chips='';
    s.server.split(' ').forEach(function(p){var kv=p.split('=');
      if(kv.length==2&&kv[0]!='up')chips+='<span class="chip">'+esc(kv[0])+' <b>'+esc(kv[1])+'</b></span>'});
    document.getElementById('chips').innerHTML=chips;
    ['cache','upstream','stale','servfail','nxdomain'].forEach(function(k){
      document.getElementById('n-'+k).textContent=(s.counts[k]||0).toLocaleString()});
    document.getElementById('cache-n').textContent=s.cache.length+' entries';

    table('cache',['Name / type','Addresses','TTL (s)~','Remaining (s)~','State'],s.cache.map(function(c){
      var st=c.negative?badge('negative','neg'):badge(c.state,c.state);
      return '<tr><td class="mono">'+esc(c.key)+'</td><td class="mono">'+(c.addrs?esc(c.addrs):'-')+'</td><td class="num">'+esc(c.ttl)+
             '</td><td class="num">'+esc(c.remaining)+'</td><td>'+st+'</td></tr>'}),'The cache is empty.');

    table('est',['Name / type','Observations~','Real changes~','Watched (s)~','Est. changes / hour~','Stale limit'],s.estimates.map(function(e){
      var lim=e.stale_limit_s, w=Math.max(0,Math.min(100,lim/CAP*100));
      var bar=lim<=0?badge('no stale allowance','none'):
        '<div class="bar"><div class="track"><div class="fill" style="width:'+w+'%"></div></div><span class="mono">'+esc(lim)+' s</span></div>';
      return '<tr><td class="mono">'+esc(e.key)+'</td><td class="num">'+esc(e.observations)+'</td><td class="num">'+esc(e.real_changes)+
             '</td><td class="num">'+esc(e.watched_s)+'</td><td class="num">'+esc(e.changes_per_hour)+'</td><td>'+bar+'</td></tr>'}),'No history yet.');

    table('dec',['#~','t (s)~','Name / type','Action','Reason','Stale age (s)~','Limit (s)~','Policy'],s.decisions.map(function(d){
      return '<tr><td class="num">'+esc(d.id)+'</td><td class="num">'+esc(d.t)+'</td><td class="mono">'+esc(d.key)+'</td><td>'+badge(d.action,d.action)+
             '</td><td>'+esc(d.reason)+'</td><td class="num">'+val(d.stale_age)+'</td><td class="num">'+val(d.limit)+'</td><td>'+esc(d.policy)+'</td></tr>'}),'No decisions logged yet.');

    document.getElementById('dot').className='dot';
    document.getElementById('upd').textContent='updated '+new Date().toLocaleTimeString();
  }).catch(function(){document.getElementById('dot').className='dot off';document.getElementById('upd').textContent='connection lost'})
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
