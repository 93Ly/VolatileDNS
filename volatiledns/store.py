"""SQLite storage (component 6): cache snapshot, answer history, decision log."""
import sqlite3
import threading


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS cache(
            key TEXT PRIMARY KEY, addrs TEXT, ttl INTEGER, stored_at REAL,
            expires_at REAL, negative INTEGER);
        CREATE TABLE IF NOT EXISTS history(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, key TEXT,
            addrs TEXT, real_change INTEGER);
        CREATE TABLE IF NOT EXISTS decisions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, key TEXT, action TEXT,
            reason TEXT, stale_age REAL, stale_limit REAL, rate REAL, policy TEXT);
        """)
        self.db.commit()

    def _run(self, sql, args=()):
        with self.lock:
            self.db.execute(sql, args)
            self.db.commit()

    def upsert_cache(self, key, e):
        self._run("INSERT OR REPLACE INTO cache VALUES (?,?,?,?,?,?)",
                  (key, ",".join(sorted(e.addrs)), e.ttl, e.stored_at, e.expires_at,
                   int(e.negative)))

    def log_history(self, ts, key, addrs, changed):
        self._run("INSERT INTO history(ts,key,addrs,real_change) VALUES (?,?,?,?)",
                  (ts, key, ",".join(sorted(addrs)), int(changed)))

    def log_decision(self, ts, key, action, reason, stale_age, limit, rate, policy):
        self._run("INSERT INTO decisions(ts,key,action,reason,stale_age,stale_limit,rate,policy) "
                  "VALUES (?,?,?,?,?,?,?,?)",
                  (ts, key, action, reason, stale_age, limit, rate, policy))
