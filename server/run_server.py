#!/usr/bin/env python3
"""Start the TBox cloud server.   python run_server.py   (see README for environment variables)"""
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "packages" / "modbus"))

from tbox_server.app import App, setup_logging  # noqa: E402
from tbox_server.config import Config  # noqa: E402


def main():
    cfg = Config.from_env()
    cfg.ensure_dirs()
    setup_logging(cfg, logging.DEBUG if os.environ.get("TBOX_DEBUG") else logging.INFO)
    app = App(cfg)
    if app.initial_admin_password:
        bar = "=" * 70
        print("\n%s\n  TBox first start - administrator account created\n  user:     %s\n  password: %s\n"
              "  You will be asked to change it at first login. This is shown only once.\n%s\n"
              % (bar, cfg.admin_user, app.initial_admin_password, bar), flush=True)
    app.serve()


if __name__ == "__main__":
    main()
