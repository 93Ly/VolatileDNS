# VolatileDNS

**A DNS caching proxy that serves stale answers during an outage, with a stale limit learned separately for every record.**

When a cached DNS answer has expired and the DNS server cannot be reached, a resolver has two bad options: return an error
(even though the website may still work) or reuse the old answer (which may point to a server that has moved).
Serving stale data is standard (RFC 8767) and built into Unbound, BIND and CoreDNS, but they all use **one fixed time limit for every record**.

VolatileDNS watches how often each name's IP address *really* changes and gives each name its own limit:
names that never move can be served stale for a long time, names that keep moving get little or none, and a hard cap bounds everything.

> Status: all eight components are implemented and tested; Docker/CoreDNS packaging, an improved estimator and
> real-trace evaluation are still to do.

## How it works

```
Clients ──query──▶  VolatileDNS proxy  ──query──▶  DNS server (upstream)
                    cache · history · policy
                          │ log
                       SQLite ◀── dashboard          controller ──▶ down / up / move / delete
```

1. A fresh cached answer is returned immediately with its remaining TTL.
2. Otherwise the proxy asks the DNS server (0.8 s per try, 2 tries, TCP fallback). A real `NXDOMAIN` is passed on and always wins over any cached address.
3. If the server gives no usable answer, the policy decides whether the expired answer may still be used. If yes it is sent with TTL 30 s and Extended DNS Error 3 (*Stale Answer*); if not, the client gets `SERVFAIL`.
4. A failing server is not asked about the same name more than once every 30 s (RFC 8767 failure recheck).

**The policy.** A new address never seen before for a name counts as a real change (reordering or CDN rotation inside a known pool does not).
The change rate is estimated as `λ = (α + c) / (β + T)`, and an expired answer is served only while the chance that it has already changed stays below a risk level ε (5 % by default):

```
stale limit = min( S_cap , −ln(1−ε) / λ − TTL )
```

Four policies can be compared on identical inputs: `none`, `fixed`, `ttl` (multiple of the record's TTL) and `volatile`.

## Results

200 randomised outage experiments (virtual time, simulated DNS server, scored against a ground-truth record of which addresses are live),
all names and outage lengths pooled, default settings:

| Policy | Working answer % | Dead address % |
|---|---|---|
| No stale answers | 5.3 | 0.3 |
| Fixed window | 80.1 | 19.9 |
| TTL-based window | 54.0 | 10.8 |
| **VolatileDNS** | **61.5** | **0.4** |

- About 98 % fewer wrong answers than the fixed window, at 18.6 points less reachability.
- A real `NXDOMAIN` was never replaced by a stale address (0 of 88,800 queries).
- **Limitation:** if a long-stable service moves during the outage, no history-based policy can know it; VolatileDNS is then as wrong as the fixed window for that service.
- The test world is synthetic, and two of its six names are built so that their TTL is misleading. See the report for the breakdown.

## Quick start

Requires Python 3.10+.

```
pip install -r requirements.txt

python -m testbed.auth_server --port 5301 --control-port 5302     # test DNS server (college.test)
python -m volatiledns --policy volatile --listen 127.0.0.1:5300 --upstream 127.0.0.1:5301

python -m testbed.controller down      # start an outage
python -m testbed.controller up        # end it
python -m dashboard.server --db volatiledns.sqlite --port 8601     # open http://127.0.0.1:8601/
```

Query it with any DNS client, for example `dig @127.0.0.1 -p 5300 portal.college.test`.

```
python -m pytest tests                 # 32 tests, including real UDP/TCP sockets
python -m testbed.experiments          # all scenario experiments, writes results/ (about 7 min)
python -m testbed.live_bench           # latency and recovery on real sockets (about 1.5 min)
```

## Repository layout

| Path | What it is |
|---|---|
| `volatiledns/` | the proxy: listener, cache and resolution engine, upstream client, history and estimator, policy, SQLite store |
| `testbed/auth_server.py` | test authoritative DNS server with an on/off switch and a control port |
| `testbed/controller.py` | fault-injection controller (down / up / move / delete) |
| `testbed/simulate.py`, `experiments.py` | virtual-time experiments: the real engine against a simulated server and a ground-truth world |
| `testbed/live_bench.py` | latency and recovery measured on real sockets |
| `dashboard/server.py` | monitoring web page (standard library only) |
| `tests/` | unit tests and integration tests over real sockets |
| `results/` | raw results, summary and figures produced by the experiments |

## Scope and limitations

- A caching proxy to one upstream server, not a recursive resolver. Stale logic applies to `A` and `AAAA` only; no DNSSEC.
- The proxy only answers clients from configured test networks, so it cannot act as an open resolver.
- The experiments use a Python test DNS server and a virtual clock; only the live benchmark uses real sockets, and only on loopback.
- To do: Docker Compose with CoreDNS and `tc netem`, an estimator that allows several changes between lookups, evaluation on real DNS traces.

## References

RFC 1034, RFC 2308, RFC 8767 (serve-stale), RFC 8914 (Extended DNS Errors); Ballani and Francis, *Mitigating DNS DoS attacks* (CCS 2008);
Xiao et al., *Estimating DNS source and cache dynamics* (ACM TOMPECS 2025); Moura et al., *When the dike breaks* (IMC 2018);
US 10,721,117 B2 (Verisign), which has per-record limits set by the domain owner. The full list is in the project report.
