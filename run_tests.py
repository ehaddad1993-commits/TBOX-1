#!/usr/bin/env python3
"""Run the whole automated test suite:  python run_tests.py"""
import sys
import unittest
from pathlib import Path

if __name__ == "__main__":
    tests = Path(__file__).resolve().parent / "tests"
    sys.path.insert(0, str(tests))
    suite = unittest.defaultTestLoader.discover(str(tests), pattern="test_*.py", top_level_dir=str(tests))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
