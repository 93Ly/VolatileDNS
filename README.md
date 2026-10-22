# VolatileDNS

**A DNS caching proxy that serves stale answers during an outage, with a stale limit learned separately for every record.**

When a cached DNS answer has expired and the DNS server cannot be reached, a resolver has two bad options: return an error
(even though the website may still work) or reuse the old answer (which may point to a server that has moved).
Serving stale data is standard (RFC 8767) and built into Unbound, BIND and CoreDNS, but they all use **one fixed time limit for every record**.

VolatileDNS watches how often each name's IP address *really* changes and gives each name its own limit:
names that never move can be served stale for a long time, names that keep moving get little or none, and a hard cap bounds everything.

## How it works

```
Clients ──query──▶  VolatileDNS proxy  ──query──▶  DNS server (upstream)
                    cache · history · policy
                          │ log
                       SQLite ◀── dashboard          controller / netem ──▶ down · up · move · delete
```

1. A fresh cached answer is returned immediately with its remaining TTL.
2. Otherwise the proxy asks the DNS server (0.8 s per try, 2 tries, TCP fallback). Identical concurrent queries share one lookup. A real `NXDOMAIN` is passed on and always wins over any cached address.
3. If the server gives no usable answer, the policy decides whether the expired answer may still be used. If yes it is sent with TTL 30 s and Extended DNS Error 3 (*Stale Answer*); if not, the client gets `SERVFAIL`.
4. A failing server is not asked about the same name more than once per recheck time (RFC 8767). Cache and history are kept in SQLite and reloaded after a restart.

**The policy.** A new address never seen before for a name counts as a real change (reordering or CDN rotation inside a known pool does not).
The change rate is estimated as `λ = (α + c) / (β + T)`, and an expired answer is served only while the chance that it has already changed stays below a risk level ε (5 % by default):

```
stale limit = min( S_cap , −ln(1−ε) / λ − TTL )
```

An interval-censored estimator (`--estimator censored`) corrects the fact that a cache sees a record only when it refreshes it. In a study over random change histories it stays within about 2 % of the true rate, while the simple estimator is up to 75 % too low when lookups are far apart.
Four policies can be compared on identical inputs: `none`, `fixed`, `ttl` (multiple of the record's TTL) and `volatile`.

## Results

200 randomised outage simulations (virtual time, ground-truth world), all names and outage lengths pooled, default settings:

| Policy | Working answer % | Dead address % |
|---|---|---|
| No stale answers | 5.3 | 0.3 |
| Fixed window | 80.1 | 19.9 |
| TTL-based window | 54.0 | 10.8 |
| **VolatileDNS** | **61.5** | **0.4** |

- About 98 % fewer wrong answers than the fixed window, at 18.6 points less reachability.
- A real `NXDOMAIN` was never replaced by a stale address (simulation and Docker lab).
- An end-to-end run in the Docker lab (CoreDNS, a real `netem` outage, real HTTP connections as ground truth) shows the same ordering of the policies.
- **Limitation:** if a long-stable service moves during the outage, no history-based policy can know it; VolatileDNS is then as wrong as the fixed window for that service.
- The simulated world is synthetic; a trace of real DNS answers (`results/traces/`) shows that most popular names stay stable for hours, so the practical gain depends on how many volatile names a network serves.

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
python -m pytest tests                 # 37 tests, including real UDP/TCP sockets
python -m testbed.experiments          # 200-seed scenario experiments, writes results/ (about 12 min)
python -m testbed.live_bench           # latency and recovery on real sockets
python -m testbed.resource_bench       # memory per cached name, throughput
python -m testbed.estimator_study      # bias of the two estimators
```

### Docker lab

CoreDNS (zone reloaded every second), a `netem` sidecar that cuts CoreDNS's network, nginx back ends that give real HTTP ground truth, a `tcpdump` sidecar, and the proxy:

```
cd docker && docker compose up -d --build
cd .. && python -m testbed.docker_experiment --policies none fixed ttl volatile
```

### Real DNS trace

```
python -m testbed.trace_collect --minutes 150 --interval 30     # poll 130 well-known domains
python -m testbed.trace_replay --trace results/traces/real_trace.jsonl
```

## Repository layout

| Path | What it is |
|---|---|
| `volatiledns/` | the proxy: listener, cache and resolution engine, upstream client, history and estimators, policy, SQLite store |
| `testbed/auth_server.py`, `controller.py` | test authoritative DNS server with an on/off switch, and the fault-injection controller |
| `testbed/simulate.py`, `experiments.py` | virtual-time experiments: the real engine against a simulated server and a ground-truth world |
| `testbed/docker_experiment.py` | end-to-end experiment in the Docker lab |
| `testbed/trace_collect.py`, `trace_replay.py` | collect and replay a trace of real DNS answers |
| `testbed/live_bench.py`, `resource_bench.py`, `estimator_study.py` | latency, resource cost and estimator accuracy |
| `dashboard/server.py` | monitoring web page (standard library only) |
| `docker/` | Compose file, Dockerfiles, CoreDNS zone, nginx configuration |
| `tests/` | unit tests and integration tests over real sockets |
| `results/` | raw results, summaries, figures and the real DNS trace |

## Scope and limitations

- A caching proxy to one upstream server, not a recursive resolver. Stale logic applies to `A` and `AAAA` only; no DNSSEC.
- The proxy only answers clients from configured test networks, so it cannot act as an open resolver.
- Simulations use virtual time; the Docker lab uses scaled-down times (minutes instead of hours) and one run per policy.

## References

RFC 1034, RFC 2308, RFC 8767 (serve-stale), RFC 8914 (Extended DNS Errors); Ballani and Francis, *Mitigating DNS DoS attacks* (CCS 2008);
Xiao et al., *Estimating DNS source and cache dynamics* (ACM TOMPECS 2025); Moura et al., *When the dike breaks* (IMC 2018);
US 10,721,117 B2 (Verisign), which has per-record limits set by the domain owner.
