# -*- coding: utf-8 -*-
"""
Discord Music RPC (Алиас для обратной совместимости)
Перенаправляет запуск на src.rpc.
"""
import os
import sys

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(ROOT_DIR, "src")
for p in (ROOT_DIR, SRC_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from src.rpc import *

if __name__ == "__main__":
    start()
