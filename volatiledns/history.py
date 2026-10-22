"""Record history and volatility estimator (components 4 of the design).

For every name the proxy remembers the address sets it received from the
DNS server.  A *real* change is an address that was never seen before for
this name; a reordering or a rotation between addresses seen recently is
not counted (unless ``count_rotation`` is set, the "strict" variant).
"""
from collections import deque
from dataclasses import dataclass, field
from math import exp, log
from typing import Deque, FrozenSet, List, Optional


@dataclass
class NameHistory:
    first_seen: float
    last_set: FrozenSet[str]
    recent: Deque[FrozenSet[str]]
    changes: int = 0
    observations: int = 1
    last_change_at: Optional[float] = None
    last_obs: float = 0.0
    det_intervals: List[float] = field(default_factory=list)   # lengths of the intervals in which a change was seen


class HistoryTracker:
    def __init__(self, history_len=20, prior_changes=1.0, prior_seconds=3600.0,
                 count_rotation=False, estimator="counted"):
        self.estimator = estimator              # "counted" = Eqn. (1);  "censored" = Eqn. (4)
        self.history_len = history_len
        self.alpha = prior_changes
        self.beta = prior_seconds
        self.count_rotation = count_rotation
        self.names = {}

    # -- recording --------------------------------------------------------
    def observe(self, name: str, addrs, now: float) -> bool:
        """Record a fresh answer.  Returns True if it counted as a real change."""
        addrs = frozenset(addrs)
        h = self.names.get(name)
        if h is None:
            h = NameHistory(first_seen=now, last_set=addrs, last_obs=now,
                            recent=deque([addrs], maxlen=self.history_len))
            self.names[name] = h
            return False
        h.observations += 1
        changed = False
        if addrs != h.last_set:
            if self.count_rotation:
                changed = True
            else:
                seen = set().union(*h.recent) if h.recent else set()
                changed = not addrs.issubset(seen)
        h.recent.append(addrs)
        h.last_set = addrs
        if changed:
            h.changes += 1
            h.last_change_at = now
            h.det_intervals.append(max(1e-6, now - h.last_obs))
        h.last_obs = now
        return changed

    # -- estimate ----------------------------------------------------------
    def rate(self, name: str, now: float) -> float:
        """Estimated changes per second.

        counted  - Eqn. (1):  (alpha + c) / (beta + T)
        censored - Eqn. (4.5): a cache sees a record only when it refreshes it, so an
                   interval in which a change was seen may hold more than one.  Each
                   detected interval D counts as lam*D / (1 - exp(-lam*D)) changes, and
                   lam = (alpha + sum of those) / (beta + T) is iterated until it settles.
        """
        h = self.names.get(name)
        c = h.changes if h else 0
        T = max(0.0, now - h.first_seen) if h else 0.0
        lam = (self.alpha + c) / (self.beta + T)
        if self.estimator != "censored" or not h or not h.det_intervals:
            return lam
        for _ in range(60):
            expected = sum((lam * d) / (1.0 - exp(-lam * d)) if lam * d > 1e-9 else 1.0 for d in h.det_intervals)
            new = (self.alpha + expected) / (self.beta + T)
            if abs(new - lam) <= 1e-10 * lam:
                lam = new
                break
            lam = new
        return lam


def stale_limit_volatile(rate: float, ttl: float, epsilon: float) -> float:
    """Eqn. (3) without the cap:  -ln(1-eps)/lambda - TTL  (may be negative)."""
    return -log(1.0 - epsilon) / rate - ttl
