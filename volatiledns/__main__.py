"""Run the proxy:  python -m volatiledns --policy volatile --listen 127.0.0.1:5300 --upstream 127.0.0.1:5301"""
import argparse
import asyncio

from .config import Config
from .server import make_server
from .store import Store


def hostport(s):
    host, port = s.rsplit(":", 1)
    return host, int(port)


def main():
    ap = argparse.ArgumentParser(prog="volatiledns")
    ap.add_argument("--policy", default="volatile", choices=["none", "fixed", "ttl", "volatile"])
    ap.add_argument("--listen", default="127.0.0.1:5300")
    ap.add_argument("--upstream", default="127.0.0.1:5301")
    ap.add_argument("--db", default="volatiledns.sqlite")
    ap.add_argument("--s-cap", type=float, default=1200.0)
    ap.add_argument("--s-fix", type=float, default=1200.0)
    ap.add_argument("--ttl-mult", type=float, default=10.0)
    ap.add_argument("--epsilon", type=float, default=0.05)
    ap.add_argument("--strict", action="store_true", help="count rotation as change")
    a = ap.parse_args()
    lh, lp = hostport(a.listen)
    uh, up = hostport(a.upstream)
    cfg = Config(policy=a.policy, listen_host=lh, listen_port=lp, upstream_host=uh,
                 upstream_port=up, db_path=a.db, s_cap=a.s_cap, s_fix=a.s_fix,
                 ttl_mult=a.ttl_mult, epsilon=a.epsilon, count_rotation=a.strict)

    async def run():
        srv = make_server(cfg, Store(cfg.db_path))
        await srv.start()
        print(f"VolatileDNS [{cfg.policy}] listening on {lh}:{lp} (UDP+TCP), upstream {uh}:{up}", flush=True)
        await asyncio.Event().wait()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
