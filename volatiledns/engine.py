"""Core of VolatileDNS: cache manager + upstream handling + policy (components 2, 3, 4, 5).

The engine knows nothing about sockets or DNS wire format.  It works on
(name, type) keys and on an *upstream* object with one coroutine,
``query(qname, qtype)``.  That keeps the logic identical whether it is
driven by the real network listener (server.py) or by the virtual-time
experiment runner (testbed/simulate.py).
"""
import asyncio
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, FrozenSet, Optional, Tuple

from .config import Config
from .history import HistoryTracker
from .policy import Decision, decide


@dataclass
class UpstreamResult:
    kind: str                      # ok | nxdomain | nodata | fail
    addrs: FrozenSet[str] = frozenset()
    ttl: int = 0
    payload: Any = None            # the raw response, kept so it can be re-sent
    queries: int = 0               # datagrams / connections used for this lookup


@dataclass
class Entry:
    addrs: FrozenSet[str]
    ttl: int
    stored_at: float
    expires_at: float
    negative: bool = False
    payload: Any = None
    neg_kind: str = "nxdomain"     # for negative entries: nxdomain | nodata


@dataclass
class Result:
    kind: str                      # cache | upstream | stale | nxdomain | nodata | servfail
    addrs: FrozenSet[str] = frozenset()
    ttl: int = 0
    stale: bool = False
    reason: str = ""
    payload: Any = None
    upstream_queries: int = 0
    decision: Optional[Decision] = None


class Engine:
    def __init__(self, cfg: Config, upstream, clock: Callable[[], float], store=None):
        self.cfg = cfg
        self.upstream = upstream
        self.clock = clock
        self.store = store
        self.cache: Dict[Tuple[str, int], Entry] = {}
        self.history = HistoryTracker(cfg.history_len, cfg.prior_changes,
                                      cfg.prior_seconds, cfg.count_rotation, cfg.estimator)
        self._inflight: Dict[Tuple[str, int], "asyncio.Future"] = {}
        self.fail_until: Dict[Tuple[str, int], float] = {}
        self.stats = {"queries": 0, "cache_hits": 0, "upstream_ok": 0,
                      "upstream_fail": 0, "upstream_datagrams": 0,
                      "stale_served": 0, "servfail": 0, "nxdomain": 0,
                      "skipped_recheck": 0, "coalesced": 0}

    # ------------------------------------------------------------------
    async def resolve(self, qname: str, qtype: int) -> Result:
        qname = qname.lower().rstrip(".") + "."
        key = (qname, qtype)
        now = self.clock()
        self.stats["queries"] += 1
        entry = self.cache.get(key)

        # 1. fresh answer in the cache
        if entry is not None and now < entry.expires_at:
            self.stats["cache_hits"] += 1
            ttl = max(1, int(round(entry.expires_at - now)))
            if entry.negative:
                return self._log(key, Result(entry.neg_kind, ttl=ttl, reason="cached negative answer",
                                             payload=entry.payload), now, "cache")
            return self._log(key, Result("cache", entry.addrs, ttl, payload=entry.payload,
                                         reason="fresh cache hit"), now, "cache")

        # 2. ask the DNS server (unless it failed for this name very recently)
        up = None
        if now >= self.fail_until.get(key, 0.0):
            up = await self._ask_upstream(key, qname, qtype)
            self.stats["upstream_datagrams"] += up.queries
            now = self.clock()
        else:
            self.stats["skipped_recheck"] += 1

        if up is not None and up.kind in ("ok", "nodata", "nxdomain"):
            self.stats["upstream_ok"] += 1
            self.fail_until.pop(key, None)
            return self._accept(key, up, now)

        if up is not None:
            self.stats["upstream_fail"] += 1
            self.fail_until[key] = now + self.cfg.failure_recheck

        # 3. the DNS server did not give an answer: may the expired answer be used?
        if qtype in (1, 28):                                   # stale logic: A and AAAA only
            d = decide(entry, self.history, now, self.cfg, f"{qname}/{qtype}")
        else:
            d = Decision(False, "stale logic only applies to A and AAAA")
        n_up = up.queries if up is not None else 0
        if d.serve_stale:
            self.stats["stale_served"] += 1
            return self._log(key, Result("stale", entry.addrs, self.cfg.stale_ttl, True,
                                         d.reason, entry.payload, n_up, d), now, "stale")
        self.stats["servfail"] += 1
        return self._log(key, Result("servfail", reason=d.reason, upstream_queries=n_up,
                                     decision=d), now, "servfail")

    # ------------------------------------------------------------------
    async def _ask_upstream(self, key, qname, qtype) -> UpstreamResult:
        """One upstream lookup per name at a time: identical queries that arrive while one is in flight share it."""
        fut = self._inflight.get(key)
        if fut is not None:
            self.stats["coalesced"] += 1
            res = await asyncio.shield(fut)
            return replace(res, queries=0)                  # the datagrams were already counted for the first caller
        fut = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        res = UpstreamResult("fail")
        try:
            res = await self.upstream.query(qname, qtype)
            return res
        finally:
            fut.set_result(res)
            self._inflight.pop(key, None)

    def restore(self, parser=None) -> int:
        """Reload the cache and the answer history from the SQLite log (after a restart)."""
        if not self.store:
            return 0
        n = 0
        for key, addrs, ttl, stored_at, expires_at, neg, neg_kind, wire in self.store.load_cache():
            qname, qtype = key.rsplit("/", 1)
            payload = parser(wire) if (parser and wire) else None
            if not neg and payload is None:
                continue                                    # a positive answer cannot be re-sent without its message
            self.cache[(qname, int(qtype))] = Entry(frozenset(a for a in addrs.split(",") if a), ttl, stored_at,
                                                    expires_at, bool(neg), payload, neg_kind or "nxdomain")
            n += 1
        for ts, key, addrs, _ in self.store.load_history():
            self.history.observe(key, [a for a in addrs.split(",") if a], ts)
        return n

    def _accept(self, key, up: UpstreamResult, now: float) -> Result:
        if up.kind in ("nxdomain", "nodata"):
            # a real "does not exist" always wins: any old address is thrown away
            ttl = self.cfg.negative_ttl
            self.cache[key] = Entry(frozenset(), ttl, now, now + ttl, True, up.payload, up.kind)
            self.stats["nxdomain"] += 1
            return self._log(key, Result("nxdomain" if up.kind == "nxdomain" else "nodata",
                                         ttl=ttl, reason="authoritative negative answer",
                                         payload=up.payload, upstream_queries=up.queries),
                             now, "upstream")
        ttl = up.ttl
        self.cache[key] = Entry(up.addrs, ttl, now, now + ttl, False, up.payload)
        changed = self.history.observe(f"{key[0]}/{key[1]}", up.addrs, now)
        if self.store:
            self.store.log_history(now, f"{key[0]}/{key[1]}", up.addrs, changed)
        return self._log(key, Result("upstream", up.addrs, ttl, reason="fresh from DNS server",
                                     payload=up.payload, upstream_queries=up.queries), now, "upstream")

    def _log(self, key, res: Result, now: float, action: str) -> Result:
        if self.store:
            d = res.decision
            self.store.log_decision(now, f"{key[0]}/{key[1]}", res.kind, res.reason,
                                    d.stale_age if d else None, d.limit if d else None,
                                    d.rate if d else None, self.cfg.policy)
            e = self.cache.get(key)
            if e is not None and action == "upstream":          # the cache only changes when the server answered
                self.store.upsert_cache(f"{key[0]}/{key[1]}", e)
        return res
