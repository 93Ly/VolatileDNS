"""Replay a REAL DNS trace (testbed/trace_collect.py) through the VolatileDNS engine.

    python -m testbed.trace_replay --trace results/traces/real_trace.jsonl --out results/trace_summary.json

The trace holds, for ~130 well-known domains, the A-record answers seen every 30 s for a couple of hours.  The replay
makes that history the "world":
  * the upstream DNS server answers each query with the most recent real answer for that name;
  * the proxy first learns from queries during a learning period (the first `--learn` seconds);
  * then an outage of `--outage` seconds is simulated, starting at several points in the rest of the trace;
  * an answer given to a client counts as WORKING if its address appears in the real answers observed within
    +/- `--window` seconds of that moment, otherwise as DEAD (the name has moved on).
Every policy sees exactly the same trace, learning period and outages.
"""
import argparse
import asyncio
import copy
import json
import random
import statistics as st
from collections import defaultdict

from testbed.simulate import VClock
from volatiledns.config import Config
from volatiledns.engine import Engine, UpstreamResult

A = 1
POLICIES = {
    "none": dict(policy="none"),
    "fixed": dict(policy="fixed"),
    "ttl": dict(policy="ttl", ttl_mult=10.0),
    "volatile": dict(policy="volatile", epsilon=0.05),
    "volatile (censored estimator)": dict(policy="volatile", epsilon=0.05, estimator="censored"),
    "volatile (risk 20 %)": dict(policy="volatile", epsilon=0.20),
    "volatile (risk 50 %)": dict(policy="volatile", epsilon=0.50),
    "volatile (beta 600 s)": dict(policy="volatile", epsilon=0.05, prior_seconds=600.0),
    "volatile (beta 600 s, risk 20 %)": dict(policy="volatile", epsilon=0.20, prior_seconds=600.0),
    "volatile (beta 180 s, risk 20 %)": dict(policy="volatile", epsilon=0.20, prior_seconds=180.0),
}
HIST_KEYS = ("estimator", "prior_seconds", "prior_changes", "count_rotation", "history_len")


def load(path, min_obs=20):
    obs = defaultdict(list)
    ttl = defaultdict(int)
    t0 = None
    with open(path, encoding="utf8") as f:
        for line in f:
            r = json.loads(line)
            if r["rcode"] != 0 or not r["addrs"]:
                continue
            t0 = r["t"] if t0 is None else min(t0, r["t"])
            obs[r["name"]].append((r["t"], frozenset(r["addrs"])))
            ttl[r["name"]] = max(ttl[r["name"]], r["ttl"])
    out = {}
    for n, series in obs.items():
        series.sort()
        if len(series) >= min_obs:
            out[n + "."] = ([(t - t0, a) for t, a in series], max(1, min(ttl[n], 3600)))
    return out


class TraceWorld:
    def __init__(self, data):
        self.data = data
        self.times = {n: [t for t, _ in s] for n, (s, _) in data.items()}

    def answer(self, name, t):
        import bisect
        series = self.data[name][0]
        i = max(0, bisect.bisect_right(self.times[name], t) - 1)
        return series[i][1]

    def live(self, name, t, window):
        import bisect
        series = self.data[name][0]
        ts = self.times[name]
        lo, hi = bisect.bisect_left(ts, t - window), bisect.bisect_right(ts, t + window)
        out = set()
        for _, a in series[max(0, lo - 1):hi + 1]:
            out |= a
        return out


class TraceUpstream:
    def __init__(self, world, clock):
        self.world, self.clock = world, clock
        self.down = False

    async def query(self, qname, qtype):
        if self.down:
            return UpstreamResult("fail", queries=2)
        return UpstreamResult("ok", self.world.answer(qname, self.clock.t), self.world.data[qname][1], None, 1)


async def learn(engine, clk, names, t_end, poll):
    t = 0.0
    n = len(names)
    while t < t_end:
        for i, name in enumerate(names):
            clk.t = t + i * poll / n
            await engine.resolve(name, A)
        t += poll


async def outage(engine, clk, up, world, names, start, dur, poll, window, rng):
    counts = defaultdict(lambda: dict(ok=0, wrong=0, servfail=0, stale=0, total=0))
    up.down = True
    t = start
    n = len(names)
    while t < start + dur:
        for i, name in enumerate(names):
            clk.t = t + i * poll / n
            res = await engine.resolve(name, A)
            c = counts[name]
            c["total"] += 1
            if res.kind in ("cache", "upstream", "stale"):
                pick = rng.choice(sorted(res.addrs))
                c["ok" if pick in world.live(name, clk.t, window) else "wrong"] += 1
                c["stale"] += res.kind == "stale"
            else:
                c["servfail"] += 1
        t += poll
    return counts


def run(trace, learn_s, outage_s, starts, poll, window, seed):
    data = load(trace)
    names = sorted(data)
    world = TraceWorld(data)
    horizon = min(s[-1][0] for s, _ in data.values())
    summary = {}
    per_name = defaultdict(lambda: defaultdict(lambda: dict(ok=0, wrong=0, servfail=0, stale=0, total=0)))
    base = Config(s_cap=1200.0)
    for s_i, start in enumerate(starts):
        if start + outage_s > horizon:
            continue
        clk = VClock()
        up = TraceUpstream(world, clk)
        learned = {}
        for label, kw in POLICIES.items():
            hv = tuple(sorted((k, kw[k]) for k in HIST_KEYS if k in kw))
            if hv not in learned:
                clk_h = VClock()
                up_h = TraceUpstream(world, clk_h)
                eng_h = Engine(base.with_(**dict(hv)), up_h, clk_h)
                asyncio.run(learn(eng_h, clk_h, names, start, poll))
                learned[hv] = (eng_h, clk_h)
            src_eng, src_clk = learned[hv]
            clk_ = VClock()
            clk_.t = src_clk.t
            up_ = TraceUpstream(world, clk_)
            eng = Engine(base.with_(**{k: v for k, v in kw.items()}), up_, clk_)
            eng.cache, eng.history = copy.deepcopy(src_eng.cache), copy.deepcopy(src_eng.history)
            rng = random.Random(seed * 1000 + s_i)
            counts = asyncio.run(outage(eng, clk_, up_, world, names, start, outage_s, poll, window, rng))
            for name, c in counts.items():
                for k, v in c.items():
                    per_name[label][name][k] += v
    for label, d in per_name.items():
        tot = defaultdict(int)
        for c in d.values():
            for k, v in c.items():
                tot[k] += v
        T = tot["total"] or 1
        summary[label] = dict(queries=tot["total"], working=100 * tot["ok"] / T, dead=100 * tot["wrong"] / T,
                              servfail=100 * tot["servfail"] / T, stale_served=tot["stale"],
                              stale_dead_pct=100 * tot["wrong"] / max(1, tot["stale"]))
    return summary, per_name, data


def classify(data):
    """Observed volatility of each name: real changes (new address ever seen) per hour over the whole trace."""
    out = {}
    for n, (series, ttl) in data.items():
        seen, ch = set(series[0][1]), 0
        for _, a in series[1:]:
            if not a <= seen:
                ch += 1
                seen |= a
        out[n] = dict(real_changes=ch, hours=(series[-1][0] - series[0][0]) / 3600.0, ttl=ttl,
                      distinct_addrs=len(seen), observations=len(series))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", default="results/traces/real_trace.jsonl")
    ap.add_argument("--out", default="results/trace_summary.json")
    ap.add_argument("--learn", type=float, default=5400.0)
    ap.add_argument("--outage", type=float, default=600.0)
    ap.add_argument("--poll", type=float, default=30.0)
    ap.add_argument("--window", type=float, default=300.0)
    ap.add_argument("--starts", type=float, nargs="*", default=None)
    a = ap.parse_args()
    data = load(a.trace)
    horizon = min(s[-1][0] for s, _ in data.values())
    starts = a.starts or [a.learn + k * 300.0 for k in range(0, 20)]
    starts = [s for s in starts if s + a.outage <= horizon]
    summary, per_name, data = run(a.trace, a.learn, a.outage, starts, a.poll, a.window, 1)
    cls = classify(data)
    by_class = {}
    for label, d in per_name.items():
        by_class[label] = {}
        for klass, pred in (("stable", lambda n: cls[n]["real_changes"] == 0), ("changed", lambda n: cls[n]["real_changes"] > 0)):
            tot = defaultdict(int)
            for n, c in d.items():
                if pred(n):
                    for k, v in c.items():
                        tot[k] += v
            T = tot["total"] or 1
            by_class[label][klass] = dict(queries=tot["total"], working=100 * tot["ok"] / T, dead=100 * tot["wrong"] / T, servfail=100 * tot["servfail"] / T)
    res = dict(trace=a.trace, names=len(data), horizon_s=horizon, learn_s=a.learn, outage_s=a.outage, outage_starts=len(starts),
               window_s=a.window, summary=summary, volatility=cls, by_class=by_class)
    json.dump(res, open(a.out, "w"), indent=1)
    print(f"{len(data)} names, horizon {horizon / 3600:.2f} h, {len(starts)} outages of {a.outage:.0f} s after a {a.learn:.0f} s learning period")
    for k, v in summary.items():
        print(f"  {k:32s} working {v['working']:5.1f}%  dead {v['dead']:5.2f}%  servfail {v['servfail']:5.1f}%  stale {v['stale_served']}")
    chg = sorted(cls.items(), key=lambda kv: -kv[1]["real_changes"])
    print("most volatile:", [(n.rstrip('.'), v["real_changes"]) for n, v in chg[:8]])
    print("names with zero real changes:", sum(1 for v in cls.values() if v["real_changes"] == 0), "of", len(cls))


if __name__ == "__main__":
    main()
