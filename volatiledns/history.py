"""Record history and volatility estimator (components 4 of the design).

For every name the proxy remembers the address sets it received from the
DNS server.  A *real* change is an address that was never seen before for
this name; a reordering or a rotation between addresses seen recently is
not counted (unless ``count_rotation`` is set, the "strict" variant).
"""
from collections import deque
from dataclasses import dataclass, field
from math import log
from typing import Deque, FrozenSet, Optional


@dataclass
class NameHistory:
    first_seen: float
    last_set: FrozenSet[str]
    recent: Deque[FrozenSet[str]]
    changes: int = 0
    observations: int = 1
    last_change_at: Optional[float] = None


class HistoryTracker:
    def __init__(self, history_len=20, prior_changes=1.0, prior_seconds=3600.0,
                 count_rotation=False):
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
            h = NameHistory(first_seen=now, last_set=addrs,
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
        return changed

    # -- estimate ----------------------------------------------------------
    def rate(self, name: str, now: float) -> float:
        """Eqn. (1):  lambda_hat = (alpha + c) / (beta + T)   [changes per second]."""
        h = self.names.get(name)
        c = h.changes if h else 0
        T = max(0.0, now - h.first_seen) if h else 0.0
        return (self.alpha + c) / (self.beta + T)


def stale_limit_volatile(rate: float, ttl: float, epsilon: float) -> float:
    """Eqn. (3) without the cap:  -ln(1-eps)/lambda - TTL  (may be negative)."""
    return -log(1.0 - epsilon) / rate - ttl
