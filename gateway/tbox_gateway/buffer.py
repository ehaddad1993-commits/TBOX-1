"""Durable store-and-forward queue (SQLite). Survives gateway restarts and internet outages."""
import json
import logging
import sqlite3
import threading

log = logging.getLogger("tbox.gateway.buffer")


class Buffer:
    def __init__(self, path, max_rows=2_000_000):
        self.max_rows = max_rows
        self._lock = threading.Lock()
        self._c = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._c.execute("PRAGMA journal_mode=WAL")
        self._c.execute("PRAGMA synchronous=NORMAL")
        self._c.execute("CREATE TABLE IF NOT EXISTS queue (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, payload TEXT NOT NULL)")
        self.dropped = 0

    def put(self, kind, payload):
        with self._lock:
            self._c.execute("INSERT INTO queue(kind,payload) VALUES(?,?)", (kind, json.dumps(payload, separators=(",", ":"))))

    def put_many(self, kind, payloads):
        if not payloads:
            return
        with self._lock:
            self._c.execute("BEGIN")
            self._c.executemany("INSERT INTO queue(kind,payload) VALUES(?,?)",
                                [(kind, json.dumps(p, separators=(",", ":"))) for p in payloads])
            self._c.execute("COMMIT")
            self._trim()

    def _trim(self):
        n = self._c.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
        if n > self.max_rows:
            excess = n - self.max_rows + self.max_rows // 20
            self._c.execute("DELETE FROM queue WHERE id IN (SELECT id FROM queue ORDER BY id LIMIT ?)", (excess,))
            self.dropped += excess
            log.error("buffer full: dropped %d oldest records (total dropped %d)", excess, self.dropped)

    def take(self, n):
        with self._lock:
            rows = self._c.execute("SELECT id, kind, payload FROM queue ORDER BY id LIMIT ?", (n,)).fetchall()
        return [(r[0], r[1], json.loads(r[2])) for r in rows]

    def delete_upto(self, last_id, first_id=0):
        with self._lock:
            self._c.execute("DELETE FROM queue WHERE id>=? AND id<=?", (first_id, last_id))

    def depth(self):
        with self._lock:
            return self._c.execute("SELECT COUNT(*) FROM queue").fetchone()[0]

    def close(self):
        with self._lock:
            self._c.close()
