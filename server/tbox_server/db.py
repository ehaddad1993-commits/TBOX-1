"""SQLite access layer: per-thread connections, WAL, explicit transactions, SQL migrations."""
import contextlib
import logging
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger("tbox.db")


def now_ms():
    return int(time.time() * 1000)


class Database:
    def __init__(self, path):
        self.path = str(path)
        self._local = threading.local()
        self.write_lock = threading.RLock()

    def conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=True)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.conn = c
        return c

    @contextlib.contextmanager
    def tx(self):
        """Serialised write transaction (re-entrant)."""
        with self.write_lock:
            c = self.conn()
            depth = getattr(self._local, "depth", 0)
            if depth == 0:
                c.execute("BEGIN IMMEDIATE")
            self._local.depth = depth + 1
            try:
                yield c
            except BaseException:
                self._local.depth -= 1
                if self._local.depth == 0:
                    c.execute("ROLLBACK")
                raise
            else:
                self._local.depth -= 1
                if self._local.depth == 0:
                    c.execute("COMMIT")

    def execute(self, sql, params=()):
        with self.write_lock:
            cur = self.conn().execute(sql, params)
            return cur

    def q(self, sql, params=()):
        return [dict(r) for r in self.conn().execute(sql, params).fetchall()]

    def one(self, sql, params=()):
        r = self.conn().execute(sql, params).fetchone()
        return dict(r) if r else None

    def scalar(self, sql, params=(), default=None):
        r = self.conn().execute(sql, params).fetchone()
        return r[0] if r and r[0] is not None else default

    def migrate(self, migrations_dir):
        c = self.conn()
        c.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at INTEGER NOT NULL)")
        done = {r[0] for r in c.execute("SELECT version FROM schema_migrations")}
        for f in sorted(Path(migrations_dir).glob("*.sql")):
            if f.stem in done:
                continue
            log.info("applying migration %s", f.stem)
            script = "BEGIN;\n%s\nINSERT INTO schema_migrations(version, applied_at) VALUES ('%s', %d);\nCOMMIT;" % (
                f.read_text(), f.stem, now_ms())
            try:
                c.executescript(script)
            except Exception:
                if c.in_transaction:
                    c.execute("ROLLBACK")
                raise

    def backup_to(self, dest):
        """Online backup using SQLite's backup API, then verify the copy."""
        dest = str(dest)
        src = self.conn()
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
        chk = sqlite3.connect(dest)
        try:
            ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            chk.close()
        if ok != "ok":
            raise RuntimeError("backup integrity check failed: %s" % ok)
        return dest

    # settings --------------------------------------------------------------
    DEFAULT_SETTINGS = {
        "retention_samples_days": 365,
        "retention_events_days": 730,
        "retention_alarms_days": 1095,
        "backup_enabled": True,
        "backup_keep": 14,
        "gateway_offline_after_s": 30,
        "control_enabled": False,
    }

    def get_setting(self, key):
        import json
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(r["value"]) if r else self.DEFAULT_SETTINGS.get(key)

    def set_setting(self, key, value):
        import json
        self.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(value)))
