"""Virtual-time experiment runner.

The *real* VolatileDNS engine (cache, history, policy) is driven by a
simulated DNS server and a simulated clock.  Nothing in the engine is
mocked; only the network and the passage of time are.  This lets us run
the same scenario under every policy, with the same random seed, hundreds
of times, in seconds instead of hours.

Ground truth: the "world" knows which IP addresses really work for every
name at every moment, so each answer the proxy gives can be scored as
  ok     - the client connects (address is live)
  wrong  - the proxy answered, but the address is dead / belongs to a moved service
  fail   - the proxy returned SERVFAIL
"""
import asyncio
import bisect
import copy
import hashlib
import random
from dataclasses import dataclass
from typing import Dict, List, Optional

from volatiledns.config import Config
from volatiledns.engine import Engine, UpstreamResult

A = 1


@dataclass
class NameSpec:
    ttl: int
    gap: Optional[float] = None       # mean seconds between real moves (None = never moves)
    rotating: bool = False            # CDN-style: answers rotate between a pool of working IPs


# The names of Table 3 in the report, plus two with a "misleading" TTL.
SPECS: Dict[str, NameSpec] = {
    "portal.college.test.":  NameSpec(ttl=120, gap=None),        # never changes
    "lab.college.test.":     NameSpec(ttl=60,  gap=4 * 3600),    # moves now and then
    "session.college.test.": NameSpec(ttl=20,  gap=300),         # moves often
    "cdn.college.test.":     NameSpec(ttl=30,  rotating=True),   # rotates between 4 working IPs
    "promo.college.test.":   NameSpec(ttl=120, gap=600),         # long TTL but moves often
    "static.college.test.":  NameSpec(ttl=20,  gap=None),        # short TTL but never moves
}


def _h(*parts) -> random.Random:
    """Deterministic RNG derived from a tuple (so every policy sees identical randomness)."""
    d = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return random.Random(int.from_bytes(d[:8], "big"))


class World:
    """Ground truth of which addresses are live for every name at every time."""

    def __init__(self, seed, specs, horizon, grace=0.0, forced=None, deleted=None):
        self.seed, self.specs, self.grace = seed, specs, grace
        self.deleted = deleted or {}
        self.events: Dict[str, List[tuple]] = {}
        self.pool = {}
        self._times = {}
        rng = random.Random(seed)
        for idx, (name, sp) in enumerate(specs.items()):
            n = 0
            ev = [(-1.0, f"10.{idx}.0.1")]
            if sp.rotating:
                self.pool[name] = [f"10.{idx}.9.{i}" for i in range(1, 5)]
            else:
                t = 0.0
                moves = []
                if sp.gap:
                    while True:
                        t += rng.expovariate(1.0 / sp.gap)
                        if t > horizon:
                            break
                        moves.append(t)
                moves += (forced or {}).get(name, [])
                for t in sorted(moves):
                    n += 1
                    ev.append((t, f"10.{idx}.{n // 200}.{n % 200 + 2}"))
            self.events[name] = ev

    def _current_index(self, name, t):
        times = self._times.get(name)
        if times is None:
            times = self._times[name] = [tt for tt, _ in self.events[name]]
        return max(0, bisect.bisect_right(times, t) - 1)

    def live(self, name, t):
        if name in self.pool:
            return set(self.pool[name])
        ev = self.events[name]
        i = self._current_index(name, t)
        live = {ev[i][1]}
        # earlier addresses stay up for `grace` seconds after the service moved away
        for k in range(i, 0, -1):
            if t < ev[k][0] + self.grace:
                live.add(ev[k - 1][1])
            else:
                break
        return live

    def answer(self, name, t):
        if name in self.pool:
            return frozenset(_h(self.seed, name, int(t * 1000)).sample(self.pool[name], 2))
        ev = self.events[name]
        return frozenset([ev[self._current_index(name, t)][1]])

    def exists(self, name, t):
        d = self.deleted.get(name)
        return d is None or t < d

    def true_changes(self, name, t0, t1):
        if name in self.pool:
            return 0
        return sum(1 for tt, _ in self.events[name][1:] if t0 <= tt <= t1)


class VClock:
    t = 0.0

    def __call__(self):
        return self.t


class SimUpstream:
    def __init__(self, world, clock, tries=2):
        self.world, self.clock, self.tries = world, clock, tries
        self.down_from = self.down_to = None

    def is_down(self, t):
        return self.down_from is not None and self.down_from <= t < self.down_to

    async def query(self, qname, qtype):
        t = self.clock.t
        if self.is_down(t):
            return UpstreamResult("fail", queries=self.tries)
        if not self.world.exists(qname, t):
            return UpstreamResult("nxdomain", queries=1)
        return UpstreamResult("ok", self.world.answer(qname, t), self.world.specs[qname].ttl, None, 1)


def clone_state(src: Engine, cfg: Config, upstream, clock) -> Engine:
    e = Engine(cfg, upstream, clock)
    e.cache = copy.deepcopy(src.cache)
    e.history = copy.deepcopy(src.history)
    e.fail_until = dict(src.fail_until)
    return e


async def _warm(engine, world, names, t_end, step):
    clk = engine.clock
    n = len(names)
    t = 0.0
    while t < t_end:
        for i, name in enumerate(names):
            clk.t = t + i * step / n
            await engine.resolve(name, A)
        t += step


def warm_engine(seed, specs, warm, history_cfg: Config, step=30.0, horizon=None, grace=0.0,
                forced=None, deleted=None):
    """Run the learning period once (it does not depend on the stale policy)."""
    names = list(specs)
    world = World(seed, specs, horizon or warm + 3000, grace, forced, deleted)
    clk = VClock()
    up = SimUpstream(world, clk, history_cfg.tries)
    eng = Engine(history_cfg, up, clk)
    asyncio.run(_warm(eng, world, names, warm, step))
    return world, eng


def outage_start(seed, warm):
    """The outage starts at a random point of the TTL cycle (same for every policy)."""
    return warm + random.Random(seed * 7919 + 1).uniform(0, 120)


def run_outage(seed, base: Engine, world: World, cfg: Config, warm, duration,
               poll=5.0, recovery=180.0):
    """One outage on a copy of the warmed-up engine under policy `cfg`.  Returns per-name counts."""
    specs = world.specs
    names = list(specs)
    clk = VClock()
    clk.t = warm
    up = SimUpstream(world, clk, cfg.tries)
    start = outage_start(seed, warm)
    end = start + duration
    up.down_from, up.down_to = start, end
    eng = clone_state(base, cfg, up, clk)
    out = {n: dict(total=0, cache_ok=0, cache_wrong=0, stale_ok=0, stale_wrong=0, servfail=0,
                   nxdomain=0, recovery=None, datagrams=0) for n in names}

    async def go():
        t = warm
        # run (without scoring) from the end of the learning period up to the outage start
        while t < start:
            for i, name in enumerate(names):
                clk.t = t + i * poll / len(names)
                if clk.t < start:
                    await eng.resolve(name, A)
            t += poll
        t = start
        stop = end + recovery
        while t < stop:
            for i, name in enumerate(names):
                clk.t = t + i * poll / len(names)
                before = eng.stats["upstream_datagrams"]
                res = await eng.resolve(name, A)
                o = out[name]
                if clk.t < end:
                    o["total"] += 1
                    o["datagrams"] += eng.stats["upstream_datagrams"] - before
                    if res.kind in ("cache", "upstream", "stale"):
                        pick = _h(seed, name, "pick", int(clk.t * 1000)).choice(sorted(res.addrs))
                        good = pick in world.live(name, clk.t)
                        key = ("stale_" if res.stale else "cache_") + ("ok" if good else "wrong")
                        o[key] += 1
                    elif res.kind == "servfail":
                        o["servfail"] += 1
                    else:
                        o["nxdomain"] += 1
                elif o["recovery"] is None and res.kind in ("upstream", "nxdomain", "nodata"):
                    o["recovery"] = clk.t - end
            t += poll

    asyncio.run(go())
    return out
