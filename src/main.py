# -*- coding: utf-8 -*-
"""
Discord Music RPC - Точка входа в приложение
"""
import os
import sys

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SRC_DIR)
for p in (ROOT_DIR, SRC_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    from src.rpc import start, main, watcher_run
except ImportError:
    from rpc import start, main, watcher_run

if __name__ == "__main__":
    start()
