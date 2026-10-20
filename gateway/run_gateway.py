#!/usr/bin/env python3
"""Start the TBox Edge Gateway.   python run_gateway.py --config gateway.json"""
import argparse
import logging
import logging.handlers
import signal
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "packages" / "modbus"))

from tbox_gateway.agent import Gateway  # noqa: E402
from tbox_gateway.config import GatewayConfig  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="TBox Edge Gateway")
    ap.add_argument("--config", default="gateway.json")
    a = ap.parse_args()
    cfg = GatewayConfig.load(a.config if Path(a.config).exists() else None)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(cfg.log_level)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    fh = logging.handlers.RotatingFileHandler(cfg.state_dir / "gateway.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(sh)
    root.addHandler(fh)
    gw = Gateway(cfg)
    gw.start()
    done = threading.Event()
    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, lambda *_: done.set())
    done.wait()
    gw.stop()


if __name__ == "__main__":
    main()
