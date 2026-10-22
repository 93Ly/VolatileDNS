"""Test authoritative DNS server for the lab zone ``college.test`` (RFC 2606 reserved name).

It plays the role CoreDNS plays in the Docker design, with one addition:
it can be switched off on demand (queries are silently dropped, TCP
connections are closed), which is what the fault-injection controller uses.

Control protocol (one text line per command, TCP):
    down | up | status
    set <name> <ip>[,<ip>...] [ttl]        (create / move a service)
    rotate <name> <ip,ip,ip,...> <k> [ttl] (answer with k random addresses of the pool)
    del <name>                             (name no longer exists -> NXDOMAIN)
    trunc <name>                           (always set TC on UDP, forcing TCP)
"""
import asyncio
import random
import struct

import dns.flags
import dns.message
import dns.rcode
import dns.rdatatype
import dns.rrset


class AuthServer:
    def __init__(self, host="127.0.0.1", port=5301, control_port=None, seed=1):
        self.host, self.port, self.control_port = host, port, control_port
        self.zone = {}
        self.up = True
        self.rng = random.Random(seed)
        self.queries = 0
        self.dropped = 0
        self.transport = None
        self._servers = []

    # -- zone editing -------------------------------------------------------
    @staticmethod
    def _n(name):
        return name.lower().rstrip(".") + "."

    def set(self, name, ips, ttl=30):
        z = self.zone.get(self._n(name), {})
        z.update(ips=list(ips), ttl=ttl, pool=None, k=0)
        self.zone[self._n(name)] = z

    def rotate(self, name, pool, k=2, ttl=30):
        self.zone[self._n(name)] = dict(ips=[], ttl=ttl, pool=list(pool), k=k)

    def delete(self, name):
        self.zone.pop(self._n(name), None)

    def trunc(self, name, on=True):
        self.zone[self._n(name)]["trunc"] = on

    # -- DNS ----------------------------------------------------------------
    def respond(self, wire, udp):
        try:
            q = dns.message.from_wire(wire)
        except Exception:
            return None
        self.queries += 1
        r = dns.message.make_response(q)
        r.flags |= dns.flags.AA
        name = q.question[0].name.to_text().lower()
        rdtype = q.question[0].rdtype
        z = self.zone.get(name)
        if z is None:
            r.set_rcode(dns.rcode.NXDOMAIN)
        elif rdtype == dns.rdatatype.A:
            ips = z["ips"] if not z.get("pool") else self.rng.sample(z["pool"], z["k"])
            if udp and z.get("trunc"):
                r.flags |= dns.flags.TC
                return r.to_wire()
            r.answer.append(dns.rrset.from_text_list(name, z["ttl"], "IN", "A", ips))
        return r.to_wire()

    async def start(self):
        loop = asyncio.get_running_loop()
        srv = self

        class UDP(asyncio.DatagramProtocol):
            def connection_made(self, t):
                srv.transport = t

            def datagram_received(self, data, addr):
                if not srv.up:
                    srv.dropped += 1
                    return
                out = srv.respond(data, True)
                if out:
                    srv.transport.sendto(out, addr)

        await loop.create_datagram_endpoint(UDP, local_addr=(self.host, self.port))
        self._servers.append(await asyncio.start_server(self._tcp, self.host, self.port))
        if self.control_port:
            self._servers.append(await asyncio.start_server(self._control, self.host, self.control_port))

    async def _tcp(self, reader, writer):
        try:
            if not self.up:
                self.dropped += 1
                return
            (n,) = struct.unpack("!H", await reader.readexactly(2))
            out = self.respond(await reader.readexactly(n), False)
            if out:
                writer.write(struct.pack("!H", len(out)) + out)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    # -- control --------------------------------------------------------------
    async def _control(self, reader, writer):
        try:
            async for raw in reader:
                writer.write((self.command(raw.decode().strip()) + "\n").encode())
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()

    def command(self, line):
        p = line.split()
        try:
            if p[0] == "down":
                self.up = False
            elif p[0] == "up":
                self.up = True
            elif p[0] == "status":
                return f"up={self.up} queries={self.queries} dropped={self.dropped} names={len(self.zone)}"
            elif p[0] == "set":
                self.set(p[1], p[2].split(","), int(p[3]) if len(p) > 3 else 30)
            elif p[0] == "rotate":
                self.rotate(p[1], p[2].split(","), int(p[3]), int(p[4]) if len(p) > 4 else 30)
            elif p[0] == "del":
                self.delete(p[1])
            elif p[0] == "trunc":
                self.trunc(p[1])
            else:
                return "ERR unknown command"
        except (IndexError, KeyError, ValueError) as e:
            return f"ERR {e!r}"
        return "OK"

    def close(self):
        if self.transport:
            self.transport.close()
        for s in self._servers:
            s.close()


def default_zone(s: AuthServer):
    """The names of Table 3 in the report."""
    s.set("portal.college.test", ["172.28.0.10"], ttl=120)
    s.set("lab.college.test", ["172.28.0.20"], ttl=60)
    s.set("session.college.test", ["172.28.0.30"], ttl=20)
    s.rotate("cdn.college.test", ["172.28.0.41", "172.28.0.42", "172.28.0.43", "172.28.0.44"], k=2, ttl=30)
    s.set("gone.college.test", ["172.28.0.50"], ttl=30)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5301)
    ap.add_argument("--control-port", type=int, default=5302)
    a = ap.parse_args()

    async def main():
        s = AuthServer(port=a.port, control_port=a.control_port)
        default_zone(s)
        await s.start()
        print(f"test DNS server on 127.0.0.1:{a.port}, control on {a.control_port}", flush=True)
        await asyncio.Event().wait()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
