"""The standard-library dashboard: state endpoint, estimates and fault-injection commands."""
import asyncio
import json
import urllib.request

from dashboard.server import serve
from testbed.auth_server import AuthServer
from volatiledns.config import Config
from volatiledns.server import make_server
from volatiledns.store import Store

import dns.asyncquery
import dns.message


def test_dashboard_state_and_controls(tmp_path):
    db = str(tmp_path / "d.sqlite")

    async def body():
        auth = AuthServer(port=5621, control_port=5622)
        auth.set("portal.college.test", ["172.28.0.10"], ttl=1)
        await auth.start()
        proxy = make_server(Config(policy="fixed", listen_port=5620, upstream_port=5621, db_path=db), Store(db))
        await proxy.start()
        for _ in range(3):
            q = dns.message.make_query("portal.college.test", "A")
            await dns.asyncquery.udp(q, "127.0.0.1", port=5620, timeout=2)
            await asyncio.sleep(1.1)
        httpd = serve(db, port=5623, control_port=5622)
        loop = asyncio.get_running_loop()

        def get(path, data=None):
            req = urllib.request.Request(f"http://127.0.0.1:5623{path}", data=data)
            with urllib.request.urlopen(req, timeout=3) as r:
                return r.read().decode()

        page = await loop.run_in_executor(None, get, "/")
        state = json.loads(await loop.run_in_executor(None, get, "/api/state"))
        assert "monitoring dashboard" in page and "<title>VolatileDNS" in page
        assert len(json.loads(await loop.run_in_executor(None, get, "/api/state"))["series"]) == 60
        assert state["counts"]["upstream"] >= 2 and "up=True" in state["server"]
        assert state["cache"][0]["key"] == "portal.college.test./1"
        est = state["estimates"][0]
        assert est["observations"] >= 2 and est["real_changes"] == 0 and est["stale_limit_s"] > 0
        await loop.run_in_executor(None, get, "/api/cmd", b"down")
        state = json.loads(await loop.run_in_executor(None, get, "/api/state"))
        assert "up=False" in state["server"]
        try:
            await loop.run_in_executor(None, get, "/api/cmd", b"rm -rf /")
            assert False, "unknown command must be rejected"
        except urllib.error.HTTPError as e:
            assert e.code == 400
        httpd.shutdown()
        proxy.close()
        auth.close()
        await asyncio.sleep(0.05)

    asyncio.run(body())
