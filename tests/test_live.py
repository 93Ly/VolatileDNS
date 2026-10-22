"""Integration tests over real UDP/TCP sockets on the loopback interface.

A real VolatileDNS listener, a real DnsUpstream (dnspython) and the test
authoritative server run in one event loop.  TTLs are 1 s so that expiry
happens quickly.
"""
import asyncio

import dns.asyncquery
import dns.edns
import dns.flags
import dns.message
import dns.rcode

from testbed.auth_server import AuthServer
from volatiledns.config import Config
from volatiledns.server import make_server
from volatiledns.store import Store

PROXY, AUTH = 5610, 5611


def cfg(**kw):
    base = dict(policy="fixed", s_fix=600, s_cap=1200, listen_port=PROXY, upstream_port=AUTH,
                try_timeout=0.25, tries=2, failure_recheck=0.5, negative_ttl=1)
    base.update(kw)
    return Config(**base)


async def with_stack(body, **kw):
    auth = AuthServer(port=AUTH)
    auth.set("portal.college.test", ["172.28.0.10"], ttl=1)
    auth.set("big.college.test", ["172.28.0.77"], ttl=1)
    auth.trunc("big.college.test")
    await auth.start()
    store = Store(":memory:")
    proxy = make_server(cfg(**kw), store)
    await proxy.start()
    try:
        return await body(auth, proxy, store)
    finally:
        proxy.close()
        auth.close()
        await asyncio.sleep(0.05)


async def ask(name="portal.college.test", rd="A", tcp=False, edns=True):
    q = dns.message.make_query(name, rd, use_edns=0 if edns else None)
    f = dns.asyncquery.tcp if tcp else dns.asyncquery.udp
    return await f(q, "127.0.0.1", port=PROXY, timeout=3)


def test_udp_and_tcp_answers():
    async def body(auth, proxy, store):
        r = await ask()
        assert r.rcode() == 0 and r.answer[0][0].address == "172.28.0.10"
        r = await ask(tcp=True)
        assert r.answer[0][0].address == "172.28.0.10"
    asyncio.run(with_stack(body))


def test_cache_answer_has_remaining_ttl_and_no_second_upstream_query():
    async def body(auth, proxy, store):
        await ask()
        n = auth.queries
        r = await ask()
        assert auth.queries == n and r.answer[0].ttl <= 1
    asyncio.run(with_stack(body))


def test_stale_answer_served_when_server_down_with_ede_3():
    async def body(auth, proxy, store):
        await ask()
        await asyncio.sleep(1.2)                    # TTL expired
        auth.up = False
        r = await ask()
        assert r.rcode() == 0 and r.answer[0][0].address == "172.28.0.10"
        assert r.answer[0].ttl == 30
        ede = [o for o in r.options if isinstance(o, dns.edns.EDEOption)]
        assert ede and ede[0].code == dns.edns.EDECode.STALE_ANSWER
        assert proxy.engine.stats["stale_served"] == 1
    asyncio.run(with_stack(body))


def test_no_stale_policy_returns_servfail():
    async def body(auth, proxy, store):
        await ask()
        await asyncio.sleep(1.2)
        auth.up = False
        r = await ask()
        assert r.rcode() == dns.rcode.SERVFAIL
    asyncio.run(with_stack(body, policy="none"))


def test_recovery_after_server_returns():
    async def body(auth, proxy, store):
        await ask()
        await asyncio.sleep(1.2)
        auth.up = False
        await ask()
        auth.set("portal.college.test", ["172.28.0.99"], ttl=1)    # service moved meanwhile
        auth.up = True
        await asyncio.sleep(0.6)                                   # failure_recheck elapsed
        r = await ask()
        assert r.answer[0][0].address == "172.28.0.99"
    asyncio.run(with_stack(body))


def test_nxdomain_passed_on_and_old_address_dropped():
    async def body(auth, proxy, store):
        await ask()
        auth.delete("portal.college.test")
        await asyncio.sleep(1.2)
        r = await ask()
        assert r.rcode() == dns.rcode.NXDOMAIN
        auth.up = False
        await asyncio.sleep(1.2)
        r = await ask()
        assert r.rcode() == dns.rcode.SERVFAIL        # never the old address
    asyncio.run(with_stack(body))


def test_upstream_tcp_fallback_when_udp_answer_truncated():
    async def body(auth, proxy, store):
        r = await ask("big.college.test")
        assert r.answer[0][0].address == "172.28.0.77"
    asyncio.run(with_stack(body))


def test_decisions_and_cache_are_logged_to_sqlite():
    async def body(auth, proxy, store):
        await ask()
        await ask()
        n = store.db.execute("select count(*) from decisions").fetchone()[0]
        kinds = {r[0] for r in store.db.execute("select action from decisions")}
        assert n == 2 and kinds == {"upstream", "cache"}
        assert store.db.execute("select count(*) from cache").fetchone()[0] == 1
    asyncio.run(with_stack(body))


def test_clients_outside_the_test_network_are_refused():
    async def body(auth, proxy, store):
        assert proxy.allowed("172.28.0.5") and proxy.allowed("127.0.0.1")
        assert not proxy.allowed("8.8.8.8") and not proxy.allowed("192.168.1.10")
    asyncio.run(with_stack(body))


def test_garbage_packet_is_dropped_without_crashing():
    async def body(auth, proxy, store):
        loop = asyncio.get_running_loop()
        tr, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol, remote_addr=("127.0.0.1", PROXY))
        tr.sendto(b"\x00\x01garbage")
        tr.close()
        await asyncio.sleep(0.1)
        r = await ask()
        assert r.rcode() == 0
    asyncio.run(with_stack(body))


def test_nodata_is_cached_as_nodata_not_nxdomain():
    async def body(auth, proxy, store):
        r1 = await ask("portal.college.test", rd="AAAA")      # name exists, no AAAA record
        r2 = await ask("portal.college.test", rd="AAAA")      # served from the negative cache
        assert r1.rcode() == 0 and not r1.answer
        assert r2.rcode() == 0 and not r2.answer
        assert proxy.engine.stats["cache_hits"] >= 1
    asyncio.run(with_stack(body))
