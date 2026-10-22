"""Configuration for VolatileDNS.

All times are in seconds.  The defaults match the design in the report:
RFC 8767 timers (0.8 s per try, 2 tries, 30 s stale TTL, 30 s failure
recheck) and a risk level of 5 %.
"""
from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class Config:
    # --- stale-answer policy -------------------------------------------
    policy: str = "volatile"        # none | fixed | ttl | volatile
    s_cap: float = 1200.0           # hard cap: no record is ever served stale longer (20 min in the lab)
    s_fix: float = 1200.0           # limit used by the "fixed" policy
    ttl_mult: float = 10.0          # M in the "ttl" policy: limit = M * TTL
    epsilon: float = 0.05           # accepted risk that the record already changed
    prior_changes: float = 1.0      # alpha in Eqn. (1)
    prior_seconds: float = 3600.0   # beta in Eqn. (1)  (alpha/beta = 1 change per hour)
    history_len: int = 20           # last answers remembered for rotation detection
    count_rotation: bool = False    # True = "strict" change detection (every difference counts)
    estimator: str = "counted"      # counted = Eqn. (1);  censored = Eqn. (4), allows several changes between lookups

    # --- RFC 8767 timers -----------------------------------------------
    stale_ttl: int = 30             # TTL put on a stale answer
    failure_recheck: float = 30.0   # do not hammer a failing upstream more often than this
    try_timeout: float = 0.8        # seconds per upstream try
    tries: int = 2                  # tries per upstream query
    negative_ttl: int = 30          # TTL used for cached NXDOMAIN / NODATA

    # --- network ---------------------------------------------------------
    listen_host: str = "127.0.0.1"
    listen_port: int = 5300
    upstream_host: str = "127.0.0.1"
    upstream_port: int = 5301
    allowed_clients: tuple = ("127.0.0.0/8", "::1/128", "172.28.0.0/24", "10.0.0.0/8")

    # --- storage ---------------------------------------------------------
    db_path: str = "volatiledns.sqlite"

    def with_(self, **kw) -> "Config":
        return replace(self, **kw)
