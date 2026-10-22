"""End-to-end experiment in the Docker lab (CoreDNS + netem + nginx back ends + the proxy).

    cd docker && docker compose up -d --build
    python -m testbed.docker_experiment --policies none fixed ttl volatile

Everything is real: DNS over UDP/TCP through the published proxy port, CoreDNS as the DNS server, `tc netem` cutting
CoreDNS's network, and a real HTTP connection (from the probe container) to the address the proxy hands out.  Times
are scaled down from the hours of a real outage (see SCALE below); the proxy's cap is 60 s and its failure recheck 10 s.

Scenario per policy
  0 .. LEARN      learning period: session moves to a new back end every MOVE_EVERY s, cdn rotates, portal never changes
  LEARN-30        the name `gone` is deleted (the proxy receives a real NXDOMAIN)
  LEARN .. +OUT   outage: netem drops everything CoreDNS sends; session keeps moving
  after           the outage ends; the proxy must return to fresh answers
Each poll asks the proxy for every name, then opens an HTTP connection to the first address of the answer.
"""
import argparse
import json
import os
import random
import subprocess
import sys
import threading
import time

import dns.edns
import dns.exception
import dns.message
import dns.query
import dns.rcode

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DOCKER = os.path.join(ROOT, "docker")
ZONE = os.path.join(DOCKER, "coredns", "db.college.test")
PROXY = ("127.0.0.1", 5380)

LEARN, OUT, REC = 150.0, 90.0, 40.0
POLL = 4.0
MOVE_EVERY = 15.0
ROTATE_EVERY = 8.0
GRACE = 3.0                                   # the old session back end stays up this long after the DNS record moves
SESSIONS = [f"172.28.0.{30 + i}" for i in range(6)]
SESSION_NAMES = [f"session-{c}" for c in "abcdef"]
CDN_POOL = [f"172.28.0.{41 + i}" for i in range(4)]
NAMES = ["portal", "session", "cdn", "gone"]
TTL = {"portal": 10, "session": 5, "cdn": 5, "gone": 10}


def dc(*args, check=True, timeout=120):
    r = subprocess.run(["docker", "compose", *args], cwd=DOCKER, capture_output=True, text=True, timeout=timeout,
                       env={**os.environ, **getattr(dc, "env", {})})
    if check and r.returncode != 0:
        raise RuntimeError(f"docker compose {' '.join(args)} failed: {r.stderr.strip()[:300]}")
    return r


class Zone:
    """The college.test zone file read by CoreDNS (which reloads it every second)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.serial = int(time.time()) % 1_000_000_000
        self.reset()

    def reset(self):
        self.state = {"portal": ["172.28.0.10"], "session": [SESSIONS[0]], "cdn": CDN_POOL[:2], "gone": ["172.28.0.10"]}
        self.write()

    def write(self):
        with self.lock:
            self.serial += 1
            lines = ["$ORIGIN college.test.",
                     f"@ 3600 IN SOA ns.college.test. admin.college.test. ( {self.serial} 7200 3600 1209600 3600 )",
                     "@ 3600 IN NS ns.college.test.", "ns 3600 IN A 172.28.0.2"]
            for name, ips in self.state.items():
                lines += [f"{name} {TTL[name]} IN A {ip}" for ip in ips]
            with open(ZONE, "w", newline="\n") as f:
                f.write("\n".join(lines) + "\n")


def probe(ips):
    """Real HTTP GET from the probe container to each address; returns {ip: bool}."""
    ips = sorted(set(ips))
    if not ips:
        return {}
    script = "for ip in %s; do if wget -q -T 1 -O /dev/null http://$ip/; then echo $ip ok; else echo $ip fail; fi; done" % " ".join(ips)
    r = dc("exec", "-T", "probe", "sh", "-c", script, check=False, timeout=60)
    out = {}
    for ln in r.stdout.split("\n"):
        if ln.strip():
            ip, res = ln.split()
            out[ip] = res == "ok"
    return out


def ask(name):
    q = dns.message.make_query(f"{name}.college.test", "A", use_edns=0)
    t = time.time()
    try:
        r = dns.query.udp(q, PROXY[0], port=PROXY[1], timeout=4)
    except (dns.exception.DNSException, OSError):
        return dict(rcode="TIMEOUT", addrs=[], ttl=None, stale=False, ms=(time.time() - t) * 1000)
    addrs = [x.to_text() for rs in r.answer for x in rs]
    ede = any(isinstance(o, dns.edns.EDEOption) and o.code == dns.edns.EDECode.STALE_ANSWER for o in r.options)
    return dict(rcode=dns.rcode.to_text(r.rcode()), addrs=addrs, ttl=r.answer[0].ttl if r.answer else None,
                stale=ede, ms=(time.time() - t) * 1000)


def timeline(zone, t0, stop):
    """Background thread: moves the session service, rotates the CDN name, deletes `gone`, cuts the network."""
    nxt_move, nxt_rot, idx = 10.0, 5.0, 0
    gone_done = out_on = out_off = False
    while not stop.is_set():
        t = time.time() - t0
        if not gone_done and t >= LEARN - 30:
            zone.state.pop("gone", None)
            zone.write()
            gone_done = True
        if not out_on and t >= LEARN:
            dc("exec", "-T", "netem", "tc", "qdisc", "replace", "dev", "eth0", "root", "netem", "loss", "100%")
            out_on = True
            zone.events.append(("outage_start", t))
        if out_on and not out_off and t >= LEARN + OUT:
            dc("exec", "-T", "netem", "tc", "qdisc", "del", "dev", "eth0", "root", check=False)
            out_off = True
            zone.events.append(("outage_end", t))
        if t >= nxt_move:
            nxt_move += MOVE_EVERY
            old, idx = idx, (idx + 1) % len(SESSIONS)
            dc("start", SESSION_NAMES[idx], check=False)
            zone.state["session"] = [SESSIONS[idx]]
            zone.write()
            zone.events.append(("move", t, SESSIONS[idx]))
            threading.Timer(GRACE, lambda n=SESSION_NAMES[old]: dc("stop", "-t", "0", n, check=False)).start()
        if t >= nxt_rot:
            nxt_rot += ROTATE_EVERY
            zone.state["cdn"] = random.sample(CDN_POOL, 2)
            zone.write()
        time.sleep(0.2)


def reset_lab(zone):
    dc("exec", "-T", "netem", "tc", "qdisc", "del", "dev", "eth0", "root", check=False)
    dc("start", *SESSION_NAMES, "portal", *[f"cdn-{i}" for i in range(1, 5)], check=False)
    for n in SESSION_NAMES[1:]:
        dc("stop", "-t", "0", n, check=False)
    zone.reset()
    time.sleep(2.5)


def start_proxy(policy, **env):
    dc.env = {"POLICY": policy, "S_CAP": "60", "S_FIX": "60", "TTL_MULT": "10", "EPS": "0.05", "PRIOR": "3600",
              "RECHECK": "10", "ESTIMATOR": "counted", **{k: str(v) for k, v in env.items()}}
    dc("up", "-d", "--force-recreate", "--no-deps", "proxy", "capture", timeout=180)
    for _ in range(40):
        if ask("portal")["rcode"] == "NOERROR":
            return
        time.sleep(0.5)
    raise RuntimeError("proxy did not come up")


def run_policy(label, policy, capture=False, **env):
    zone = Zone()
    zone.events = []
    reset_lab(zone)
    start_proxy(policy, **env)
    stop = threading.Event()
    t0 = time.time()
    th = threading.Thread(target=timeline, args=(zone, t0, stop), daemon=True)
    th.start()
    rows, capt = [], False
    print(f"[{label}] running {LEARN + OUT + REC:.0f} s ...", flush=True)
    while time.time() - t0 < LEARN + OUT + REC:
        t_round = time.time()
        t = t_round - t0
        if capture and not capt and t >= LEARN - 2:
            dc("exec", "-d", "capture", "tcpdump", "-nn", "-i", "eth0", "-c", "400", "-w", f"/captures/outage_{label}.pcap", "port", "53")
            capt = True
        answers = {n: ask(n) for n in NAMES}
        pr = probe([a["addrs"][0] for a in answers.values() if a["addrs"]])
        for n, a in answers.items():
            if a["rcode"] == "NOERROR" and a["addrs"]:
                outcome = "ok" if pr.get(a["addrs"][0]) else "wrong"
            elif a["rcode"] == "NXDOMAIN":
                outcome = "nxdomain"
            else:
                outcome = "servfail"
            phase = "learn" if t < LEARN else ("outage" if t < LEARN + OUT else "after")
            rows.append(dict(t=round(t, 1), name=n, phase=phase, outcome=outcome, stale=a["stale"], ttl=a["ttl"],
                             addr=a["addrs"][0] if a["addrs"] else None, ms=round(a["ms"], 1)))
        time.sleep(max(0.0, POLL - (time.time() - t_round)))
    stop.set()
    th.join(timeout=5)
    dc("exec", "-T", "netem", "tc", "qdisc", "del", "dev", "eth0", "root", check=False)
    if capture:
        r = dc("exec", "-T", "capture", "tcpdump", "-nn", "-tt", "-r", f"/captures/outage_{label}.pcap", check=False)
        with open(os.path.join(DOCKER, "captures", f"outage_{label}.txt"), "w") as f:
            f.write(r.stdout + r.stderr)
    stats = dc("logs", "proxy", "--no-log-prefix", check=False).stdout[-300:]
    return dict(label=label, policy=policy, env=env, rows=rows, events=zone.events, proxy_log_tail=stats)


def summarise(res):
    out = {}
    for ph in ("outage", "after"):
        for n in NAMES:
            rr = [r for r in res["rows"] if r["phase"] == ph and r["name"] == n]
            if not rr:
                continue
            c = {k: sum(1 for r in rr if r["outcome"] == k) for k in ("ok", "wrong", "servfail", "nxdomain")}
            c["stale"] = sum(1 for r in rr if r["stale"])
            c["n"] = len(rr)
            c["max_ms"] = max(r["ms"] for r in rr)
            out[f"{ph}/{n}"] = c
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--policies", nargs="+", default=["none", "fixed", "ttl", "volatile"])
    ap.add_argument("--out", default=os.path.join(ROOT, "results", "docker_results.json"))
    ap.add_argument("--learn", type=float, default=None, help="length of the learning period in seconds (default 150)")
    ap.add_argument("--history-len", type=int, default=None, help="answers remembered per name for rotation detection (default 20)")
    a = ap.parse_args()
    if a.learn:
        global LEARN
        LEARN = a.learn
    results = []
    for p in a.policies:
        label, _, est = p.partition(":")
        pol = label
        env = {"ESTIMATOR": est} if est else {}
        if a.history_len:
            env["HISTORY_LEN"] = a.history_len
        res = run_policy(p.replace(":", "_") + (f"_learn{int(a.learn)}" if a.learn else "") + (f"_hist{a.history_len}" if a.history_len else ""), pol, capture=(p in ("fixed", "volatile") and not a.learn and not a.history_len), **env)
        res["summary"] = summarise(res)
        results.append(res)
        for k, v in res["summary"].items():
            if k.startswith("outage"):
                print(f"   {k:16s} {v}", flush=True)
        with open(a.out, "w") as f:
            json.dump(results, f, indent=1)
    dc.env = {}
    print("wrote", a.out)


if __name__ == "__main__":
    sys.exit(main())
