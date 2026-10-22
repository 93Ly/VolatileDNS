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
    ap.add_argument("--estimator", default="counted", choices=["counted", "censored"])
    ap.add_argument("--prior-seconds", type=float, default=3600.0, help="beta of Eqn. (1)")
    ap.add_argument("--failure-recheck", type=float, default=30.0)
    ap.add_argument("--try-timeout", type=float, default=0.8)
    ap.add_argument("--stale-ttl", type=int, default=30)
    ap.add_argument("--no-restore", action="store_true", help="do not reload cache/history from the database")
    a = ap.parse_args()
    lh, lp = hostport(a.listen)
    uh, up = hostport(a.upstream)
    cfg = Config(policy=a.policy, listen_host=lh, listen_port=lp, upstream_host=uh,
                 upstream_port=up, db_path=a.db, s_cap=a.s_cap, s_fix=a.s_fix,
                 ttl_mult=a.ttl_mult, epsilon=a.epsilon, count_rotation=a.strict,
                 estimator=a.estimator, prior_seconds=a.prior_seconds, failure_recheck=a.failure_recheck,
                 try_timeout=a.try_timeout, stale_ttl=a.stale_ttl)

    async def run():
        srv = make_server(cfg, Store(cfg.db_path), restore=not a.no_restore)
        await srv.start()
        print(f"VolatileDNS [{cfg.policy}] listening on {lh}:{lp} (UDP+TCP), upstream {uh}:{up}", flush=True)
        await asyncio.Event().wait()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
