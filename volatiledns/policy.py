"""Stale-answer policy engine (component 5 of the design).

``decide`` is the function of Listing 1 in the report.  It is a pure
function of the cache entry, the history and the time, so every policy can
be run on exactly the same inputs.
"""
from dataclasses import dataclass
from typing import Optional

from .history import stale_limit_volatile

POLICIES = ("none", "fixed", "ttl", "volatile")


@dataclass
class Decision:
    serve_stale: bool
    reason: str
    stale_age: float = 0.0
    limit: float = 0.0
    rate: Optional[float] = None


def decide(entry, history, now, cfg, name) -> Decision:
    if entry is None:
        return Decision(False, "no cached answer")
    if entry.negative:
        return Decision(False, "negative entry is never served stale")
    if entry.ttl == 0:
        return Decision(False, "TTL 0 record")

    stale_age = now - entry.expires_at
    if stale_age > cfg.s_cap:
        return Decision(False, "beyond hard cap", stale_age, cfg.s_cap)

    rate = None
    if cfg.policy == "none":
        limit = -1.0
    elif cfg.policy == "fixed":
        limit = min(cfg.s_cap, cfg.s_fix)
    elif cfg.policy == "ttl":
        limit = min(cfg.s_cap, cfg.ttl_mult * entry.ttl)
    elif cfg.policy == "volatile":
        rate = history.rate(name, now)
        limit = min(cfg.s_cap, stale_limit_volatile(rate, entry.ttl, cfg.epsilon))
    else:
        raise ValueError(f"unknown policy {cfg.policy!r}")

    if stale_age <= limit:
        return Decision(True, "within stale limit", stale_age, limit, rate)
    return Decision(False, "over stale limit" if limit >= 0 else "stale not allowed",
                    stale_age, limit, rate)
