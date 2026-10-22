"""Short live demo used for the dashboard screenshot and the sample log in the report.

Starts the test DNS server (control port 5302) and the proxy (policy 'fixed', so stale answers are
visible), sends traffic for about 60 s with a 10 s outage in the middle, then stays alive for
`--hold` seconds so the dashboard can be opened.
"""
import argparse
import asyncio
import time

import dns.asyncquery
import dns.message

from testbed.auth_server import AuthServer
from volatiledns.config import Config
from volatiledns.server import make_server
from volatiledns.store import Store


async def ask(name, port=5300):
    q = dns.message.make_query(name, "A", use_edns=0)
    try:
        return await dns.asyncquery.udp(q, "127.0.0.1", port=port, timeout=3)
    except Exception:
        return None


async def main(db, hold, policy):
    auth = AuthServer(port=5301, control_port=5302)
    auth.set("portal.college.test", ["172.28.0.10"], ttl=2)
    auth.set("lab.college.test", ["172.28.0.20"], ttl=2)
    auth.set("session.college.test", ["172.28.0.30"], ttl=1)
    auth.rotate("cdn.college.test", ["172.28.0.41", "172.28.0.42", "172.28.0.43", "172.28.0.44"], k=2, ttl=2)
    auth.set("gone.college.test", ["172.28.0.50"], ttl=2)
    await auth.start()
    cfg = Config(policy=policy, s_fix=600, s_cap=1200, listen_port=5300, upstream_port=5301, db_path=db)
    proxy = make_server(cfg, Store(db))
    await proxy.start()
    names = ["portal", "lab", "session", "cdn", "gone"]
    t0 = time.time()
    n_sess = 0
    while time.time() - t0 < 62:
        el = time.time() - t0
        if 12 <= el < 22 and auth.up and el > 12.2:
            auth.up = False                                   # outage starts
        if el >= 22 and not auth.up:
            auth.up = True                                    # outage ends
        if el > 3 and int(el) % 4 == 0 and int(el) // 4 > n_sess:
            n_sess = int(el) // 4
            auth.set("session.college.test", [f"172.28.1.{n_sess}"], ttl=1)    # session service moves
        if 30 < el < 31:
            auth.delete("gone.college.test")
        for n in names:
            await ask(f"{n}.college.test")
        await asyncio.sleep(0.5)
    print("demo traffic finished; holding servers for", hold, "s", flush=True)
    await asyncio.sleep(hold)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="results/demo.sqlite")
    ap.add_argument("--hold", type=float, default=0)
    ap.add_argument("--policy", default="fixed")
    a = ap.parse_args()
    import os
    for ext in ("", "-wal", "-shm"):
        if os.path.exists(a.db + ext):
            os.remove(a.db + ext)
    asyncio.run(main(a.db, a.hold, a.policy))

