"""Real-time measurements over real UDP/TCP sockets (loopback).

    python -m testbed.live_bench

Uses the real RFC 8767 timers (0.8 s per try, 2 tries, 30 s failure recheck), so the
latencies are measured, not modelled.  Writes results/live_bench.json and results/live.sqlite.
"""
import asyncio
import json
import os
import statistics as st
import time

import dns.asyncquery
import dns.edns
import dns.message

from testbed.auth_server import AuthServer
from volatiledns.config import Config
from volatiledns.server import make_server
from volatiledns.store import Store

PROXY, AUTH = 5700, 5701


async def ask(name, tcp=False, port=PROXY):
    q = dns.message.make_query(name, "A", use_edns=0)
    f = dns.asyncquery.tcp if tcp else dns.asyncquery.udp
    t = time.perf_counter()
    r = await f(q, "127.0.0.1", port=port, timeout=5)
    ms = (time.perf_counter() - t) * 1000
    ede = any(isinstance(o, dns.edns.EDEOption) for o in r.options)
    addr = r.answer[0][0].address if r.answer else None
    return dict(ms=ms, rcode=r.rcode(), addr=addr, ttl=r.answer[0].ttl if r.answer else None, stale=ede)


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p / 100 * len(xs)))]


async def run(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    db = os.path.join(out_dir, "live.sqlite")
    for ext in ("", "-wal", "-shm"):
        if os.path.exists(db + ext):
            os.remove(db + ext)
    R = {}

    # ------------------------------------------------------------------ latency + throughput
    auth = AuthServer(port=AUTH)
    auth.set("bench.college.test", ["172.28.0.10"], ttl=3600)
    auth.set("portal.college.test", ["172.28.0.10"], ttl=1)
    await auth.start()
    cfg = Config(policy="fixed", s_fix=600, s_cap=1200, listen_port=PROXY, upstream_port=AUTH,
                 db_path=db)
    proxy = make_server(cfg, Store(db))
    await proxy.start()

    R["cold_miss_ms"] = (await ask("bench.college.test"))["ms"]
    hits = [(await ask("bench.college.test"))["ms"] for _ in range(1000)]
    R["cache_hit_udp_ms"] = dict(n=len(hits), median=st.median(hits), p95=pct(hits, 95), p99=pct(hits, 99), max=max(hits))
    hits_t = [(await ask("bench.college.test", tcp=True))["ms"] for _ in range(200)]
    R["cache_hit_tcp_ms"] = dict(n=len(hits_t), median=st.median(hits_t), p95=pct(hits_t, 95), max=max(hits_t))

    t = time.perf_counter()
    N, C = 4000, 40
    async def worker(k):
        for _ in range(k):
            await ask("bench.college.test")
    await asyncio.gather(*[worker(N // C) for _ in range(C)])
    R["throughput_qps"] = N / (time.perf_counter() - t)

    # ------------------------------------------------------------------ outage walk-through
    steps = []
    await ask("portal.college.test")
    await asyncio.sleep(1.2)                                   # TTL (1 s) expires
    r = await ask("portal.college.test"); steps.append(("refresh after expiry (server up)", r))
    await asyncio.sleep(1.2)
    auth.up = False
    r = await ask("portal.college.test"); steps.append(("1st query, server down (2 tries x 0.8 s)", r))
    t_fail = time.perf_counter()                                # the failure recheck timer starts here
    for i in range(5):
        r = await ask("portal.college.test")
    steps.append(("next query, within the 30 s failure-recheck window", r))
    quick = []
    for i in range(50):
        quick.append((await ask("portal.college.test"))["ms"])
    R["stale_within_recheck_ms"] = dict(median=st.median(quick), max=max(quick))
    # a service moves while the server is down; the proxy cannot know
    auth.set("portal.college.test", ["172.28.0.99"], ttl=1)
    await asyncio.sleep(max(0, 30.6 - (time.perf_counter() - t_fail)))
    r = await ask("portal.college.test"); steps.append(("after 30 s: retries the server (still down)", r))
    t_fail = time.perf_counter()
    auth.up = True
    t_up = time.perf_counter()
    recovery = None
    while time.perf_counter() - t_up < 45:
        r = await ask("portal.college.test")
        if not r["stale"] and r["addr"] == "172.28.0.99":
            recovery = time.perf_counter() - t_up
            break
        await asyncio.sleep(0.25)
    steps.append(("server back up: first fresh answer", r))
    R["recovery_s"] = recovery
    R["outage_steps"] = [dict(step=s, **v) for s, v in steps]
    R["engine_stats"] = dict(proxy.engine.stats)
    R["auth_queries"] = auth.queries
    proxy.close(); auth.close()
    await asyncio.sleep(0.2)

    # ------------------------------------------------------------------ the four policies, same outage
    pol = {}
    for p in ("none", "fixed", "ttl", "volatile"):
        auth = AuthServer(port=AUTH)
        auth.set("portal.college.test", ["172.28.0.10"], ttl=1)
        await auth.start()
        proxy = make_server(cfg.with_(policy=p, listen_port=PROXY, db_path=":memory:"), None)
        await proxy.start()
        for _ in range(3):
            await ask("portal.college.test"); await asyncio.sleep(1.1)
        auth.up = False
        r = await ask("portal.college.test")
        pol[p] = dict(outcome="stale answer" if r["stale"] else ("SERVFAIL" if r["rcode"] == 2 else "fresh"),
                      ms=r["ms"], ttl=r["ttl"])
        proxy.close(); auth.close()
        await asyncio.sleep(0.2)
    R["policies_first_query_in_outage"] = pol

    with open(os.path.join(out_dir, "live_bench.json"), "w") as f:
        json.dump(R, f, indent=1)
    return R


if __name__ == "__main__":
    R = asyncio.run(run("results"))
    print(json.dumps(R, indent=1))
