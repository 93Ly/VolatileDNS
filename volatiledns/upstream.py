"""Upstream Resolver Interface (component 3): talks to the DNS server over UDP, falls back to TCP.

A lookup ends in one of four ways:
  ok        a normal answer with addresses
  nodata    NOERROR but no records of the asked type
  nxdomain  the server says the name does not exist (a real answer, not a failure)
  fail      timeout, SERVFAIL, REFUSED, bad reply ... (the server did not give an answer)
"""
import dns.asyncquery
import dns.exception
import dns.flags
import dns.message
import dns.query
import dns.rcode
import dns.rdatatype

from .engine import UpstreamResult

ADDR_TYPES = (dns.rdatatype.A, dns.rdatatype.AAAA)


class DnsUpstream:
    def __init__(self, cfg):
        self.cfg = cfg

    async def query(self, qname: str, qtype: int) -> UpstreamResult:
        cfg = self.cfg
        q = dns.message.make_query(qname, qtype, use_edns=0, payload=1232)  # random ID
        sent = 0
        for _ in range(cfg.tries):
            sent += 1
            try:
                r = await dns.asyncquery.udp(q, cfg.upstream_host, timeout=cfg.try_timeout,
                                             port=cfg.upstream_port)
                if r.flags & dns.flags.TC:                      # answer too big for UDP: use TCP
                    sent += 1
                    r = await dns.asyncquery.tcp(q, cfg.upstream_host, timeout=2.0,
                                                 port=cfg.upstream_port)
                res = self._classify(r, qname, qtype)
                if res is not None:
                    res.queries = sent
                    return res
            except (dns.exception.Timeout, dns.exception.DNSException, OSError):
                pass
        return UpstreamResult("fail", queries=sent)

    @staticmethod
    def _classify(r, qname, qtype):
        rc = r.rcode()
        if rc == dns.rcode.NXDOMAIN:
            return UpstreamResult("nxdomain", payload=r)
        if rc != dns.rcode.NOERROR:
            return None                                          # SERVFAIL, REFUSED ... = failure
        addrs, ttl = set(), None
        for rrset in r.answer:
            ttl = rrset.ttl if ttl is None else min(ttl, rrset.ttl)
            if rrset.rdtype == qtype:
                addrs.update(rd.to_text() for rd in rrset)
        if not addrs:
            return UpstreamResult("nodata", payload=r)
        return UpstreamResult("ok", frozenset(addrs), int(ttl or 0), r)
