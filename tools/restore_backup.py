#!/usr/bin/env python3
"""Restore a TBox database backup. STOP the server first.

  python tools/restore_backup.py data/backups/tbox-20261018-020000.db [--data-dir data]
"""
import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("backup")
    ap.add_argument("--data-dir", default="data")
    a = ap.parse_args()
    src, data = Path(a.backup), Path(a.data_dir)
    if not src.is_file():
        sys.exit("Backup file not found: %s" % src)
    c = sqlite3.connect(str(src))
    try:
        res = c.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        c.close()
    if res != "ok" or not {"users", "devices", "tags", "samples"} <= tables:
        sys.exit("Backup failed verification (%s). Nothing was changed." % res)
    data.mkdir(parents=True, exist_ok=True)
    live = data / "tbox.db"
    if live.exists():
        keep = data / ("tbox.db.before-restore-%s" % time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(live, keep)
        print("Current database saved as", keep)
    for ext in ("-wal", "-shm"):
        (data / ("tbox.db" + ext)).unlink(missing_ok=True)
    shutil.copy2(src, live)
    print("Restored", src, "->", live, "\nStart the server again.")


if __name__ == "__main__":
    main()
