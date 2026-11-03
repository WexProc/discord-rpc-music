# -*- coding: utf-8 -*-
"""
Discord Music RPC
Корневой файл запуска. Запускает реализацию из папки src.
"""
import os
import sys

# Добавляем каталог src в путь поиска модулей
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(ROOT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from src.main import start

if __name__ == "__main__":
    start()
