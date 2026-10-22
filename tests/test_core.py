"""Unit tests for the history estimator, the policies and the engine logic."""
import asyncio
import math

import pytest

from volatiledns.config import Config
from volatiledns.engine import Engine, UpstreamResult
from volatiledns.history import HistoryTracker, stale_limit_volatile
from volatiledns.policy import decide
from volatiledns.engine import Entry


# ---------------------------------------------------------------- history
def test_same_addresses_different_order_is_not_a_change():
    h = HistoryTracker()
    h.observe("a", ["1.1.1.1", "2.2.2.2"], 0)
    assert h.observe("a", ["2.2.2.2", "1.1.1.1"], 10) is False
    assert h.names["a"].changes == 0


def test_new_address_is_a_real_change():
    h = HistoryTracker()
    h.observe("a", ["1.1.1.1"], 0)
    assert h.observe("a", ["9.9.9.9"], 10) is True
    assert h.names["a"].changes == 1


def test_rotation_between_known_addresses_is_not_a_change():
    h = HistoryTracker()
    pool = ["1.1.1.1", "2.2.2.2", "3.3.3.3", "4.4.4.4"]
    h.observe("cdn", pool[:2], 0)
    h.observe("cdn", pool[2:], 10)            # 3.3.3.3 never seen: a change (learning the pool)
    before = h.names["cdn"].changes
    h.observe("cdn", [pool[0], pool[3]], 20)  # all seen before: rotation
    h.observe("cdn", [pool[1], pool[2]], 30)
    assert h.names["cdn"].changes == before


def test_strict_mode_counts_every_difference():
    h = HistoryTracker(count_rotation=True)
    h.observe("cdn", ["1.1.1.1"], 0)
    h.observe("cdn", ["2.2.2.2"], 1)
    h.observe("cdn", ["1.1.1.1"], 2)
    assert h.names["cdn"].changes == 2


def test_rate_equation_1():
    h = HistoryTracker(prior_changes=1, prior_seconds=3600)
    assert h.rate("unknown", 0) == pytest.approx(1 / 3600)            # no data: the prior
    h.observe("a", ["1.1.1.1"], 0)
    # watched 7 days, no changes: 1 / (3600 + 604800)
    assert h.rate("a", 604800) == pytest.approx(1 / (3600 + 604800))
    h.observe("a", ["2.2.2.2"], 100)
    assert h.rate("a", 864000) == pytest.approx(2 / (3600 + 864000))


# ---------------------------------------------------------------- worked example of the report (Eqn. 3)
@pytest.mark.parametrize("gap,expected_hours", [(7 * 86400, 8.5), (86400, 1.15)])
def test_report_worked_example(gap, expected_hours):
    lim = stale_limit_volatile(1 / gap, ttl=300, epsilon=0.05)
    assert lim / 3600 == pytest.approx(expected_hours, abs=0.1)


def test_changing_every_hour_gets_no_stale_allowance():
    assert stale_limit_volatile(1 / 3600, ttl=300, epsilon=0.05) < 0


# ---------------------------------------------------------------- policy
def entry(ttl=300, expires=1000.0, negative=False):
    return Entry(frozenset(["1.1.1.1"]), ttl, expires - ttl, expires, negative)


def hist_with_gap(gap, now):
    h = HistoryTracker(prior_changes=0.0001, prior_seconds=1)    # make the prior negligible
    h.observe("n/1", ["1.1.1.1"], 0)
    n = int(now / gap)
    for i in range(n):
        h.observe("n/1", [f"9.9.{i // 200}.{i % 200}"], (i + 1) * gap)
    return h


def test_none_policy_never_serves_stale():
    cfg = Config(policy="none")
    assert not decide(entry(), HistoryTracker(), 1001, cfg, "n/1").serve_stale


def test_fixed_policy_respects_window_and_cap():
    cfg = Config(policy="fixed", s_fix=600, s_cap=1200)
    assert decide(entry(), HistoryTracker(), 1000 + 500, cfg, "n/1").serve_stale
    assert not decide(entry(), HistoryTracker(), 1000 + 700, cfg, "n/1").serve_stale
    cfg = Config(policy="fixed", s_fix=5000, s_cap=1200)             # cap wins
    assert not decide(entry(), HistoryTracker(), 1000 + 1300, cfg, "n/1").serve_stale


def test_ttl_policy_is_multiple_of_ttl():
    cfg = Config(policy="ttl", ttl_mult=2, s_cap=1200)
    e = entry(ttl=100, expires=1000)
    assert decide(e, HistoryTracker(), 1150, cfg, "n/1").serve_stale
    assert not decide(e, HistoryTracker(), 1250, cfg, "n/1").serve_stale


def test_volatile_policy_long_for_stable_none_for_volatile():
    cfg = Config(policy="volatile", s_cap=10**7, epsilon=0.05)
    stable = hist_with_gap(10**9, 10**5)
    volatile = hist_with_gap(120, 10**5)
    e = entry(ttl=60, expires=10**5)
    assert decide(e, stable, 10**5 + 3000, cfg, "n/1").serve_stale
    assert not decide(e, volatile, 10**5 + 1, cfg, "n/1").serve_stale


def test_negative_and_zero_ttl_never_stale():
    cfg = Config(policy="fixed")
    assert not decide(entry(negative=True), HistoryTracker(), 1001, cfg, "n/1").serve_stale
    assert not decide(entry(ttl=0), HistoryTracker(), 1001, cfg, "n/1").serve_stale
    assert not decide(None, HistoryTracker(), 1001, cfg, "n/1").serve_stale


# ---------------------------------------------------------------- engine
class FakeUpstream:
    def __init__(self):
        self.mode = "ok"
        self.addrs = frozenset(["1.1.1.1"])
        self.ttl = 60
        self.calls = 0

    async def query(self, qname, qtype):
        self.calls += 1
        if self.mode == "fail":
            return UpstreamResult("fail", queries=2)
        if self.mode == "nxdomain":
            return UpstreamResult("nxdomain", queries=1)
        return UpstreamResult("ok", self.addrs, self.ttl, None, 1)


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def make(policy="fixed", **kw):
    clk, up = Clock(), FakeUpstream()
    return Engine(Config(policy=policy, **kw), up, clk), up, clk


def run(coro):
    return asyncio.run(coro)


def test_miss_then_fresh_hit_uses_cache():
    e, up, clk = make()
    assert run(e.resolve("a.test", 1)).kind == "upstream"
    clk.t += 10
    r = run(e.resolve("a.test", 1))
    assert r.kind == "cache" and r.ttl == 50 and up.calls == 1


def test_expired_and_server_down_serves_stale_with_ttl_30():
    e, up, clk = make("fixed", s_fix=600)
    run(e.resolve("a.test", 1))
    clk.t += 100                      # expired 40 s ago
    up.mode = "fail"
    r = run(e.resolve("a.test", 1))
    assert r.kind == "stale" and r.stale and r.ttl == 30


def test_stale_denied_beyond_cap_gives_servfail():
    e, up, clk = make("fixed", s_fix=10**6, s_cap=100)
    run(e.resolve("a.test", 1))
    clk.t += 60 + 150
    up.mode = "fail"
    assert run(e.resolve("a.test", 1)).kind == "servfail"


def test_nxdomain_is_never_replaced_by_stale_address():
    e, up, clk = make("fixed")
    run(e.resolve("a.test", 1))
    clk.t += 100
    up.mode = "nxdomain"
    assert run(e.resolve("a.test", 1)).kind == "nxdomain"
    clk.t += 100                       # negative entry expired too, server now down
    up.mode = "fail"
    assert run(e.resolve("a.test", 1)).kind == "servfail"


def test_failure_recheck_timer_limits_upstream_load():
    e, up, clk = make("fixed", s_fix=600, failure_recheck=30)
    run(e.resolve("a.test", 1))
    clk.t += 100
    up.mode = "fail"
    run(e.resolve("a.test", 1))
    calls = up.calls
    for _ in range(5):                 # 5 more queries within 30 s: no new upstream attempts
        clk.t += 5
        run(e.resolve("a.test", 1))
    assert up.calls == calls
    clk.t += 10                        # now > 30 s since the failure: one more attempt
    run(e.resolve("a.test", 1))
    assert up.calls == calls + 1


def test_recovery_returns_fresh_answer_after_server_returns():
    e, up, clk = make("fixed")
    run(e.resolve("a.test", 1))
    clk.t += 100
    up.mode = "fail"
    run(e.resolve("a.test", 1))
    up.mode, up.addrs = "ok", frozenset(["2.2.2.2"])
    clk.t += 31
    r = run(e.resolve("a.test", 1))
    assert r.kind == "upstream" and r.addrs == frozenset(["2.2.2.2"])


def test_stale_logic_only_for_a_and_aaaa():
    e, up, clk = make("fixed")
    run(e.resolve("a.test", 15))       # MX
    clk.t += 100
    up.mode = "fail"
    assert run(e.resolve("a.test", 15)).kind == "servfail"
