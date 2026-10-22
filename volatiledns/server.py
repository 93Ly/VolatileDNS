"""DNS Query Listener (component 1): UDP and TCP on the listen port.

Only clients from the configured test networks are answered (the proxy must
not become an open resolver).  Replies keep the client's query ID; stale
answers carry TTL 30 and, if the client speaks EDNS, Extended DNS Error 3.
"""
import asyncio
import ipaddress
import struct
import time

import dns.edns
import dns.exception
import dns.flags
import dns.message
import dns.rcode
import dns.rdatatype

from .engine import Engine, Result


def build_response(query: dns.message.Message, res: Result) -> dns.message.Message:
    resp = dns.message.make_response(query)
    resp.flags |= dns.flags.RA
    if res.kind in ("cache", "upstream", "stale"):
        for rrset in res.payload.answer:
            rr = rrset.copy()
            rr.ttl = res.ttl
            resp.answer.append(rr)
        if res.stale and query.edns >= 0:
            ede = dns.edns.EDEOption(dns.edns.EDECode.STALE_ANSWER, "served stale by VolatileDNS")
            resp.use_edns(0, 0, query.payload, options=[ede])
    elif res.kind == "nxdomain":
        resp.set_rcode(dns.rcode.NXDOMAIN)
    elif res.kind == "servfail":
        resp.set_rcode(dns.rcode.SERVFAIL)
    return resp                                                  # "nodata": empty NOERROR


def to_wire_fit(resp: dns.message.Message, query: dns.message.Message) -> bytes:
    size = query.payload if query.edns >= 0 else 512
    try:
        return resp.to_wire(max_size=size)
    except dns.exception.TooBig:
        tc = dns.message.make_response(query)
        tc.flags |= dns.flags.TC | dns.flags.RA
        return tc.to_wire()


class ProxyServer:
    def __init__(self, engine: Engine, cfg):
        self.engine, self.cfg = engine, cfg
        self.nets = [ipaddress.ip_network(n) for n in cfg.allowed_clients]
        self.transport = None
        self.tcp_server = None
        self.refused = 0

    def allowed(self, host: str) -> bool:
        try:
            ip = ipaddress.ip_address(host.split("%")[0])
        except ValueError:
            return False
        return any(ip in n for n in self.nets)

    async def answer(self, wire: bytes, udp: bool):
        try:
            query = dns.message.from_wire(wire)
        except dns.exception.DNSException:
            return None                                          # malformed: drop
        if query.opcode() != 0 or len(query.question) != 1:
            r = dns.message.make_response(query)
            r.set_rcode(dns.rcode.NOTIMP if query.opcode() != 0 else dns.rcode.FORMERR)
            return r.to_wire()
        q = query.question[0]
        res = await self.engine.resolve(q.name.to_text(), q.rdtype)
        resp = build_response(query, res)
        return to_wire_fit(resp, query) if udp else resp.to_wire()

    # -- UDP ---------------------------------------------------------------
    async def start(self):
        loop = asyncio.get_running_loop()
        server = self

        class UDP(asyncio.DatagramProtocol):
            def connection_made(self, transport):
                server.transport = transport

            def datagram_received(self, data, addr):
                if not server.allowed(addr[0]):
                    server.refused += 1
                    return
                asyncio.ensure_future(server._udp_task(data, addr))

        await loop.create_datagram_endpoint(UDP, local_addr=(self.cfg.listen_host, self.cfg.listen_port))
        self.tcp_server = await asyncio.start_server(self._tcp_client, self.cfg.listen_host,
                                                     self.cfg.listen_port)

    async def _udp_task(self, data, addr):
        out = await self.answer(data, udp=True)
        if out is not None:
            self.transport.sendto(out, addr)

    # -- TCP ---------------------------------------------------------------
    async def _tcp_client(self, reader, writer):
        peer = writer.get_extra_info("peername")
        if not self.allowed(peer[0]):
            self.refused += 1
            writer.close()
            return
        try:
            while True:
                hdr = await asyncio.wait_for(reader.readexactly(2), timeout=10)
                (n,) = struct.unpack("!H", hdr)
                data = await asyncio.wait_for(reader.readexactly(n), timeout=10)
                out = await self.answer(data, udp=False)
                if out is None:
                    break
                writer.write(struct.pack("!H", len(out)) + out)
                await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    def close(self):
        if self.transport:
            self.transport.close()
        if self.tcp_server:
            self.tcp_server.close()


def make_server(cfg, store=None, upstream=None, clock=time.time):
    from .upstream import DnsUpstream
    engine = Engine(cfg, upstream or DnsUpstream(cfg), clock, store)
    return ProxyServer(engine, cfg)
