#!/usr/bin/env python3
"""Run the Modbus TCP simulator meter on port 1502 (for testing the real Modbus path)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "modbus"))
from tbox_modbus.sim import main
main()
