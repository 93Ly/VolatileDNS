"""SQLite storage (component 6): cache snapshot, answer history, decision log."""
import sqlite3
import threading


class Store:
    """flush_interval > 0: writes are committed in batches every `flush_interval` seconds (fast);
    flush_interval == 0: every write is committed at once (safest, much slower)."""

    def __init__(self, path: str, flush_interval: float = 0.25):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.flush_interval = flush_interval
        self._dirty = False
        self._stop = threading.Event()
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS cache(
            key TEXT PRIMARY KEY, addrs TEXT, ttl INTEGER, stored_at REAL,
            expires_at REAL, negative INTEGER, neg_kind TEXT, wire BLOB);
        CREATE TABLE IF NOT EXISTS history(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, key TEXT,
            addrs TEXT, real_change INTEGER);
        CREATE TABLE IF NOT EXISTS decisions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, key TEXT, action TEXT,
            reason TEXT, stale_age REAL, stale_limit REAL, rate REAL, policy TEXT);
        """)
        for col in ("neg_kind TEXT", "wire BLOB"):               # databases written by earlier versions
            try:
                self.db.execute("ALTER TABLE cache ADD COLUMN " + col)
            except sqlite3.OperationalError:
                pass
        self.db.commit()
        if flush_interval > 0:
            threading.Thread(target=self._flusher, daemon=True).start()

    def _flusher(self):
        while not self._stop.wait(self.flush_interval):
            self.flush()

    def flush(self):
        with self.lock:
            if self._dirty:
                self.db.commit()
                self._dirty = False

    def close(self):
        self._stop.set()
        self.flush()
        self.db.close()

    def _run(self, sql, args=()):
        with self.lock:
            self.db.execute(sql, args)
            if self.flush_interval <= 0:
                self.db.commit()
            else:
                self._dirty = True

    def upsert_cache(self, key, e):
        wire = e.payload.to_wire() if hasattr(e.payload, "to_wire") else None
        self._run("INSERT OR REPLACE INTO cache VALUES (?,?,?,?,?,?,?,?)",
                  (key, ",".join(sorted(e.addrs)), e.ttl, e.stored_at, e.expires_at,
                   int(e.negative), getattr(e, "neg_kind", "nxdomain"), wire))

    def load_cache(self):
        with self.lock:
            return self.db.execute("select key, addrs, ttl, stored_at, expires_at, negative, neg_kind, wire from cache").fetchall()

    def load_history(self):
        with self.lock:
            return self.db.execute("select ts, key, addrs, real_change from history order by id").fetchall()

    def log_history(self, ts, key, addrs, changed):
        self._run("INSERT INTO history(ts,key,addrs,real_change) VALUES (?,?,?,?)",
                  (ts, key, ",".join(sorted(addrs)), int(changed)))

    def log_decision(self, ts, key, action, reason, stale_age, limit, rate, policy):
        self._run("INSERT INTO decisions(ts,key,action,reason,stale_age,stale_limit,rate,policy) "
                  "VALUES (?,?,?,?,?,?,?,?)",
                  (ts, key, action, reason, stale_age, limit, rate, policy))
