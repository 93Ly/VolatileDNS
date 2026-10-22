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


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>VolatileDNS dashboard</title>
<style>
body{font-family:Georgia,'Times New Roman',serif;margin:18px;color:#111;background:#fff}
h1{font-size:22px;margin:0 0 10px} h2{font-size:15px;margin:16px 0 6px;border-bottom:1px solid #999}
table{border-collapse:collapse;width:100%;font-size:12px} th,td{border:1px solid #999;padding:3px 6px;text-align:left}
th{background:#eee} .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.card{border:1px solid #999;padding:6px 12px;min-width:90px} .card b{font-size:20px;display:block}
button{font:inherit;padding:4px 10px;border:1px solid #333;background:#f4f4f4;cursor:pointer}
.down{color:#b00020;font-weight:bold} .up{color:#0a6b2d;font-weight:bold}
.stale{background:#fff2cc} .servfail{background:#f8d7da}
</style></head><body>
<h1>VolatileDNS monitoring dashboard</h1>
<div class="row"><div class="card">Test DNS server<b id="srv">?</b><span id="srvtxt" style="font-size:11px"></span></div>
<button onclick="cmd('down')">Take server down</button><button onclick="cmd('up')">Bring server up</button>
<span>Move service:</span><select id="mname"><option>lab.college.test</option><option>portal.college.test</option>
<option>session.college.test</option></select><input id="mip" value="172.28.0.99" size="12">
<button onclick="cmd('set '+mname.value+' '+mip.value+' 60')">Move</button></div>
<h2>Decisions so far</h2><div class="row" id="counts"></div>
<h2>Cache</h2><table id="cache"></table>
<h2>Record history and volatility estimate</h2><table id="est"></table>
<h2>Last 40 decisions</h2><table id="dec"></table>
<script>
function tbl(id,cols,rows,cls){var h='<tr>'+cols.map(c=>'<th>'+c[1]+'</th>').join('')+'</tr>';
 rows.forEach(r=>{h+='<tr class="'+(cls?cls(r):'')+'">'+cols.map(c=>'<td>'+(r[c[0]]===null?'-':r[c[0]])+'</td>').join('')+'</tr>'});
 document.getElementById(id).innerHTML=h}
function cmd(c){fetch('/api/cmd',{method:'POST',body:c}).then(load)}
function load(){fetch('/api/state').then(r=>r.json()).then(s=>{
 var down=s.server.indexOf('up=False')>=0, ok=s.server.indexOf('up=True')>=0;
 srv.textContent=down?'DOWN':(ok?'UP':'?'); srv.className=down?'down':'up'; srvtxt.textContent=s.server;
 counts.innerHTML=['cache','upstream','stale','servfail','nxdomain'].map(k=>'<div class="card">'+k+'<b>'+(s.counts[k]||0)+'</b></div>').join('');
 tbl('cache',[['key','Name/type'],['addrs','Addresses'],['ttl','TTL (s)'],['remaining','Remaining TTL (s)'],['state','State'],['negative','Negative']],s.cache);
 tbl('est',[['key','Name/type'],['observations','Observations'],['real_changes','Real changes'],['watched_s','Watched (s)'],['changes_per_hour','Est. changes / hour'],['stale_limit_s','Stale limit, VolatileDNS (s)']],s.estimates);
 tbl('dec',[['id','#'],['t','t (s)'],['key','Name/type'],['action','Action'],['reason','Reason'],['stale_age','Stale age (s)'],['limit','Limit (s)'],['policy','Policy']],s.decisions,r=>r.action);
})}
load(); setInterval(load,2000);
</script></body></html>"""


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
