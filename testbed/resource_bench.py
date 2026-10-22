"""Resource cost of the proxy logic: memory per cached name, engine throughput, SQLite log growth.

    python -m testbed.resource_bench          # writes results/resource_bench.json

Memory is the Python heap measured with tracemalloc (plus the process working set on Windows).  Throughput is the
number of engine.resolve() calls per second on cache hits, without sockets, so it shows what the logic costs.
"""
import asyncio
import ctypes
import json
import os
import tempfile
import time
import tracemalloc

from testbed.simulate import VClock
from volatiledns.config import Config
from volatiledns.engine import Engine, UpstreamResult
from volatiledns.store import Store


class Up:
    async def query(self, qname, qtype):
        return UpstreamResult("ok", frozenset(["10.0.0.1", "10.0.0.2"]), 300, None, 1)


def working_set_mb():
    try:
        class PMC(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong), ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
        return pmc.WorkingSetSize / 1e6
    except Exception:
        return None


async def fill(engine, clk, n):
    for i in range(n):
        await engine.resolve(f"host{i}.college.test", 1)


async def hits(engine, clk, names, rounds):
    t = time.perf_counter()
    c = 0
    for _ in range(rounds):
        for nm in names:
            await engine.resolve(nm, 1)
            c += 1
    return c / (time.perf_counter() - t)


def main():
    out = {}
    clk = VClock()
    base_ws = working_set_mb()
    for n in (1000, 10000):
        tracemalloc.start()
        before = tracemalloc.get_traced_memory()[0]
        eng = Engine(Config(policy="volatile"), Up(), clk)
        asyncio.run(fill(eng, clk, n))
        cur = tracemalloc.get_traced_memory()[0] - before
        tracemalloc.stop()
        out[f"heap_bytes_per_name_{n}"] = cur / n
        out[f"heap_mb_{n}"] = cur / 1e6
    # throughput on cache hits
    eng = Engine(Config(policy="volatile"), Up(), clk)
    names = [f"host{i}.college.test" for i in range(1000)]
    asyncio.run(fill(eng, clk, 1000))
    out["engine_hits_per_s"] = asyncio.run(hits(eng, clk, [n + "." if not n.endswith(".") else n for n in names], 100))
    # throughput with the SQLite decision log switched on: commit every write, and batched commits
    for label, fi in (("commit_each", 0.0), ("batched", 0.25)):
        d = tempfile.mkdtemp()
        db = os.path.join(d, f"bench_{label}.sqlite")
        st = Store(db, flush_interval=fi)
        eng2 = Engine(Config(policy="volatile"), Up(), clk, st)
        asyncio.run(fill(eng2, clk, 1000))
        t = time.perf_counter()
        rounds = 3 if fi == 0 else 100
        asyncio.run(hits(eng2, clk, names, rounds))
        out[f"engine_hits_per_s_log_{label}"] = rounds * 1000 / (time.perf_counter() - t)
        st.flush()
        rows = st.db.execute("select count(*) from decisions").fetchone()[0]
        st.db.execute("PRAGMA wal_checkpoint(FULL)")
        out["sqlite_bytes_per_decision"] = os.path.getsize(db) / max(1, rows)
        out["sqlite_rows_" + label] = rows
    os.makedirs("results", exist_ok=True)
    json.dump(out, open("results/resource_bench.json", "w"), indent=1)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
